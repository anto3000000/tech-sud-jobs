"""ATS adapters.

Each adapter knows how to:
  * be *detected* from a careers page (HTML body + final URL + headers),
  * extract the canonical org *slug* from that page,
  * *fetch* the open jobs for a slug via a public JSON endpoint (when one exists).

Recos this file implements:
  #1  A vocabulary of ATS wider than greenhouse/lever/ashby
      (taleez, recruitee, workable, welcometothejungle, icims, smartrecruiters, personio, teamtailor).
  #3  For FR / PACA scale-ups the "long tail" ATS matter as much as the US big three,
      so they get first-class adapters here, not an afterthought.

`fetch()` returns a FetchResult. When an ATS has no usable public API
(icims, welcometothejungle, teamtailor, custom) the adapter still detects it
and returns method="browser" so the pipeline can route it to a headless fetch.
"""
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

from .http import get_json, get_text, post_json


# --------------------------------------------------------------------------- #
#  data types
# --------------------------------------------------------------------------- #
class Job:
    __slots__ = ("title", "location", "department", "contract", "remote", "url",
                 "published_at", "description", "raw")

    def __init__(self, title, url, location=None, department=None, contract=None,
                 remote=None, published_at=None, description=None, raw=None):
        self.title = (title or "").strip()
        self.url = url
        self.location = location
        self.department = department
        self.contract = contract
        self.remote = remote
        self.published_at = published_at
        self.description = description
        self.raw = raw or {}

    def as_dict(self):
        d = {
            "title": self.title,
            "location": self.location,
            "department": self.department,
            "contract": self.contract,
            "remote": self.remote,
            "url": self.url,
            "published_at": self.published_at,
        }
        if self.description:
            d["description"] = self.description
        return d


class FetchResult:
    def __init__(self, ats, slug, ok, jobs=None, endpoint=None, method="api", note=None):
        self.ats = ats
        self.slug = slug
        self.ok = ok
        self.jobs = jobs or []
        self.endpoint = endpoint
        self.method = method          # "api" | "browser" | "none"
        self.note = note

    def as_dict(self):
        return {
            "ats": self.ats,
            "slug": self.slug,
            "ok": self.ok,
            "method": self.method,
            "endpoint": self.endpoint,
            "note": self.note,
            "job_count": len(self.jobs),
            "jobs": [j.as_dict() for j in self.jobs],
        }


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def slugify(name, sep="-"):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", sep, s).strip(sep).lower()
    return s


def slug_variants(name, declared_slug=None):
    """Conservative slug guesses. No aggressive first-token heuristic: a bare
    'arm' / 'median' / 'paradox' would collide with unrelated public boards."""
    out = []
    if declared_slug:
        out.append(declared_slug.strip().lower())
    # strip common parenthetical / suffix noise: "Navya (Chasset)" -> "Navya"
    clean = re.sub(r"\(.*?\)", "", name)
    clean = re.sub(
        r"\b(group|groupe|france|sud|technologies|technology|solutions?|consulting|"
        r"software|systems?|syst[eè]mes?|digital|inc|sa|sas)\b",
        "", clean, flags=re.I)
    for base in (clean, name):
        for v in (slugify(base, "-"), slugify(base, "")):
            if v and v not in out:
                out.append(v)
    return out


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first(regexes, text):
    for rx in regexes:
        m = re.search(rx, text, re.I)
        if m:
            return m.group(1)
    return None


def _strip_html(s):
    """HTML job body -> plain text with blank-line paragraphs / "• " bullets,
    the shape render_pages.text_to_html expects (mirrors sources/wttj_enrich)."""
    s = re.sub(r"<(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _body(*parts, heads=None):
    """Join a run of HTML fragments (optionally prefixed by <h4> headings) into
    one plain-text job body. `heads` is a matching list of section titles; an
    empty / falsy fragment is skipped along with its heading."""
    chunks = []
    for i, frag in enumerate(parts):
        if not frag:
            continue
        h = (heads[i] if heads and i < len(heads) else None)
        chunks.append(("<h4>%s</h4>" % h if h else "") + frag)
    return _strip_html("\n\n".join(chunks)) or None


# --------------------------------------------------------------------------- #
#  adapter base
# --------------------------------------------------------------------------- #
class Adapter:
    key = "base"
    # substrings that, if present in the careers-page HTML / final URL / headers,
    # identify this ATS
    signatures = ()
    # regexes whose group(1) is the canonical slug, tried against the HTML
    slug_regexes = ()
    has_api = False

    def detect(self, html, final_url, headers):
        blob = (html or "") + "\n" + (final_url or "") + "\n" + "\n".join(
            "%s: %s" % (k, v) for k, v in (headers or {}).items()
        )
        blob = blob.lower()
        return any(sig in blob for sig in self.signatures)

    def extract_slug(self, html, final_url):
        return _first(self.slug_regexes, (html or "") + "\n" + (final_url or ""))

    def fetch(self, slug, careers_origin=None):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Greenhouse
# --------------------------------------------------------------------------- #
class Greenhouse(Adapter):
    key = "greenhouse"
    has_api = True
    # EU-hosted boards live on *.eu.greenhouse.io (e.g. job-boards.eu.greenhouse.io/iothink).
    signatures = ("boards.greenhouse.io", "job-boards.greenhouse.io",
                  "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
                  "boards-api.greenhouse.io", "boards-api.eu.greenhouse.io",
                  "grnhse", "greenhouse.io/embed")
    slug_regexes = (
        r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
        r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([a-z0-9_-]+)",
        r"grnhse\.io/([a-z0-9_-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        for host in ("boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"):
            url = "https://%s/v1/boards/%s/jobs?content=true" % (host, slug)
            r = get_json(url, retries=1)
            if r.ok:
                break
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url,
                               note="HTTP %s" % r.status)
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name")
            depts = ", ".join(d.get("name", "") for d in j.get("departments", []) if d)
            # `content` is HTML with the entities double-escaped (&lt;p&gt;)
            jobs.append(Job(j.get("title"), j.get("absolute_url"), location=loc,
                            department=depts or None, published_at=j.get("updated_at"),
                            description=_body(unescape(j.get("content") or "")), raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url)


# --------------------------------------------------------------------------- #
#  Lever
# --------------------------------------------------------------------------- #
class Lever(Adapter):
    key = "lever"
    has_api = True
    signatures = ("jobs.lever.co", "api.lever.co", "lever-client", "cdn.lever.co")
    slug_regexes = (
        r"jobs\.lever\.co/([a-z0-9_-]+)",
        r"api\.lever\.co/v0/postings/([a-z0-9_-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        url = "https://api.lever.co/v0/postings/%s?mode=json" % slug
        r = get_json(url, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
        jobs = []
        for j in r.json():
            cat = j.get("categories") or {}
            # opening blurb + each titled list + closing "additional" block
            lists = "".join(
                "<h4>%s</h4>%s" % (lst.get("text") or "", lst.get("content") or "")
                for lst in (j.get("lists") or []))
            desc = _body(j.get("description") or j.get("opening"), lists, j.get("additional"))
            jobs.append(Job(j.get("text"), j.get("hostedUrl"),
                            location=cat.get("location"),
                            department=cat.get("team") or cat.get("department"),
                            contract=cat.get("commitment"),
                            published_at=_iso_ms(j.get("createdAt")),
                            description=desc, raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url)


# --------------------------------------------------------------------------- #
#  Ashby
# --------------------------------------------------------------------------- #
class Ashby(Adapter):
    key = "ashby"
    has_api = True
    signatures = ("jobs.ashbyhq.com", "ashbyhq.com", "api.ashbyhq.com")
    slug_regexes = (
        r"jobs\.ashbyhq\.com/([a-z0-9_-]+)",
        r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        url = "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % slug
        r = get_json(url, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
        jobs = []
        for j in r.json().get("jobs", []):
            desc = j.get("descriptionPlain") or _body(j.get("descriptionHtml"))
            jobs.append(Job(j.get("title"), j.get("jobUrl") or j.get("applyUrl"),
                            location=j.get("location"),
                            department=j.get("department") or j.get("team"),
                            contract=j.get("employmentType"),
                            remote=j.get("isRemote"),
                            published_at=j.get("publishedAt"),
                            description=(desc or "").strip() or None, raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url)


# --------------------------------------------------------------------------- #
#  Recruitee
# --------------------------------------------------------------------------- #
class Recruitee(Adapter):
    key = "recruitee"
    has_api = True
    signatures = ("recruitee.com", ".recruitee.com/api")
    slug_regexes = (
        r"([a-z0-9-]+)\.recruitee\.com",
        r"recruitee\.com/(?:c|o)/([a-z0-9-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        url = "https://%s.recruitee.com/api/offers/" % slug
        r = get_json(url, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
        jobs = []
        for j in r.json().get("offers", []):
            loc = j.get("location") or ", ".join(
                x for x in (j.get("city"), j.get("country")) if x
            )
            jobs.append(Job(j.get("title"), j.get("careers_url") or j.get("careers_apply_url"),
                            location=loc or None,
                            department=j.get("department"),
                            contract=j.get("employment_type_code") or j.get("kind"),
                            remote=j.get("remote"),
                            published_at=j.get("published_at"),
                            description=_body(j.get("description"), j.get("requirements"),
                                              heads=(None, "Profil recherché")),
                            raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url)


# --------------------------------------------------------------------------- #
#  Workable
# --------------------------------------------------------------------------- #
class Workable(Adapter):
    key = "workable"
    has_api = True
    signatures = ("apply.workable.com", "workable.com/api", "Powered byWorkable",
                  "workable.com/spi")
    slug_regexes = (
        r"apply\.workable\.com/([a-z0-9-]+)",
        r"workable\.com/spi/v3/accounts/([a-z0-9-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        url = "https://apply.workable.com/api/v3/accounts/%s/jobs" % slug
        r = post_json(url, {"query": "", "location": [], "department": [],
                            "worktype": [], "remote": []}, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
        data = r.json()
        jobs = []
        for j in data.get("results", []):
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                          loc.get("country")) if x)
            shortcode = j.get("shortcode") or j.get("id")
            jurl = "https://apply.workable.com/%s/j/%s/" % (slug, shortcode) if shortcode else None
            jobs.append(Job(j.get("title"), jurl, location=loc_s or None,
                            department=j.get("department"),
                            contract=j.get("employment_type"),
                            remote=bool(loc.get("workplace") == "remote") or None,
                            published_at=j.get("published_on") or j.get("created_at"), raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url,
                           note="0 offers" if not jobs else None)


# --------------------------------------------------------------------------- #
#  SmartRecruiters
# --------------------------------------------------------------------------- #
class SmartRecruiters(Adapter):
    key = "smartrecruiters"
    has_api = True
    signatures = ("careers.smartrecruiters.com", "jobs.smartrecruiters.com",
                  "api.smartrecruiters.com", "smartrecruiters.com/job")
    slug_regexes = (
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9-]+)",
        r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9-]+)",
    )

    # the postings list carries no body; the per-posting detail does, under
    # jobAd.sections {companyDescription, jobDescription, qualifications,
    # additionalInformation}. Few postings per company -> one extra GET each.
    _SECTIONS = ("companyDescription", "jobDescription", "qualifications",
                 "additionalInformation")

    def _ad_body(self, slug, jid):
        if not jid:
            return None
        r = get_json("https://api.smartrecruiters.com/v1/companies/%s/postings/%s"
                     % (slug, jid), retries=0)
        if not r.ok:
            return None
        secs = ((r.json().get("jobAd") or {}).get("sections") or {})
        return _body(*[(secs.get(k) or {}).get("text") for k in self._SECTIONS])

    def fetch(self, slug, careers_origin=None):
        out, offset = [], 0
        endpoint = "https://api.smartrecruiters.com/v1/companies/%s/postings" % slug
        while True:
            url = "%s?limit=100&offset=%d" % (endpoint, offset)
            r = get_json(url, retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, False, endpoint=endpoint,
                                   note="HTTP %s" % r.status)
            data = r.json()
            for j in data.get("content", []):
                loc = j.get("location") or {}
                loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                              loc.get("country")) if x)
                jid = j.get("id")
                jurl = "https://jobs.smartrecruiters.com/%s/%s" % (slug, jid) if jid else None
                out.append(Job(j.get("name"), jurl, location=loc_s or None,
                               department=(j.get("department") or {}).get("label"),
                               contract=(j.get("typeOfEmployment") or {}).get("label"),
                               remote=loc.get("remote"),
                               published_at=j.get("releasedDate"),
                               description=self._ad_body(slug, jid), raw=j))
            total = data.get("totalFound", len(out))
            offset += 100
            if offset >= total or not data.get("content"):
                break
        return FetchResult(self.key, slug, True, out, endpoint=endpoint,
                           note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  Personio
# --------------------------------------------------------------------------- #
class Personio(Adapter):
    key = "personio"
    has_api = True
    signatures = ("jobs.personio.de", "jobs.personio.com", "personio.de/job", "personio.com/job")
    slug_regexes = (
        r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)",
    )

    def fetch(self, slug, careers_origin=None):
        for tld in ("de", "com"):
            url = "https://%s.jobs.personio.%s/xml" % (slug, tld)
            r = get_text(url, retries=0)
            if not r.ok or "<position" not in r.body:
                continue
            try:
                root = ET.fromstring(r.body.encode("utf-8"))
            except ET.ParseError:
                continue
            jobs = []
            for pos in root.iter("position"):
                def g(tag):
                    el = pos.find(tag)
                    return el.text.strip() if el is not None and el.text else None
                pid = g("id")
                jurl = "https://%s.jobs.personio.%s/job/%s" % (slug, tld, pid) if pid else None
                # <jobDescriptions><jobDescription><name/><value/> (value = HTML)
                secs, heads = [], []
                jd = pos.find("jobDescriptions")
                for d in (jd.findall("jobDescription") if jd is not None else []):
                    nm, vl = d.find("name"), d.find("value")
                    if vl is not None and vl.text:
                        heads.append(nm.text.strip() if nm is not None and nm.text else None)
                        secs.append(vl.text)
                jobs.append(Job(g("name"), jurl, location=g("office"),
                                department=g("department"),
                                contract=g("employmentType"),
                                remote=None, published_at=g("createdAt"),
                                description=_body(*secs, heads=heads), raw={}))
            return FetchResult(self.key, slug, True, jobs, endpoint=url)
        return FetchResult(self.key, slug, False, endpoint="jobs.personio.*", note="no feed")


# --------------------------------------------------------------------------- #
#  Taleez  (public API needs a key; the self-hosted careers front proxies it
#           unauthenticated at  {careers_origin}/api/careez  or  /api/jobs)
# --------------------------------------------------------------------------- #
class Taleez(Adapter):
    key = "taleez"
    has_api = True
    signatures = ("taleez.com", "files.taleez.com", "/api/careez", "taleezhq")
    slug_regexes = (
        r"([a-z0-9][a-z0-9-]*)\.taleez\.com",                 # <slug>.taleez.com hosted front
        r"taleez\.com/(?:careers|jobs|widget)/([a-z0-9-]+)",
        r"files\.taleez\.com/files/(\d+)/",   # org id fallback
    )
    _GENERIC = {"www", "app", "files", "api", "cdn", "static"}

    def extract_slug(self, html, final_url):
        for rx in self.slug_regexes:
            for m in re.finditer(rx, (html or "") + "\n" + (final_url or ""), re.I):
                cand = m.group(1).lower()
                if cand not in self._GENERIC:
                    return cand
        return None

    def fetch(self, slug, careers_origin=None):
        origins = []
        if careers_origin:
            origins.append(careers_origin.rstrip("/"))
        # the fingerprint often lands on the company site, not the Taleez front —
        # <slug>.taleez.com proxies /api/careez unauthenticated.
        if slug and not slug.isdigit() and slug not in self._GENERIC:
            tz = "https://%s.taleez.com" % slug
            if tz not in origins:
                origins.append(tz)
        for origin in origins:
            for path in ("/api/careez", "/api/jobs"):
                url = origin + path
                r = get_json(url, retries=0)
                if not r.ok:
                    continue
                try:
                    data = r.json()
                except ValueError:
                    continue
                raw_jobs = data.get("jobs") if isinstance(data, dict) else data
                if not isinstance(raw_jobs, list):
                    continue
                jobs = []
                for j in raw_jobs:
                    loc = j.get("location") or {}
                    loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
                    jslug = j.get("slug")
                    jurl = "%s/j/%s" % (origin, jslug) if jslug else origin
                    jobs.append(Job(j.get("label") or j.get("title"), jurl,
                                    location=loc_s or None,
                                    department=loc.get("department"),
                                    contract=j.get("contract"),
                                    remote=j.get("remote"),
                                    published_at=_iso_ms(j.get("publishDate")), raw=j))
                return FetchResult(self.key, slug, True, jobs, endpoint=url)
        return FetchResult(self.key, slug, False, endpoint="{origin}/api/careez",
                           method="browser",
                           note="needs the company's Taleez careers host (careers_origin)")


# --------------------------------------------------------------------------- #
#  Detection-only adapters (no clean public JSON API -> route to browser)
# --------------------------------------------------------------------------- #
class WelcomeToTheJungle(Adapter):
    key = "welcometothejungle"
    has_api = False
    signatures = ("welcometothejungle.com", "welcomekit.co", "wttj", "csekhvms53")
    slug_regexes = (
        r"welcometothejungle\.com/[a-z]{2}/companies(?:-v1)?/([a-z0-9-]+)",
        r"welcomekit\.co/[a-z]{2}/companies/([a-z0-9-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        return FetchResult(
            self.key, slug, False, method="browser",
            endpoint="https://www.welcometothejungle.com/fr/companies/%s/jobs" % slug,
            note="jobs served via rotating Algolia key -> use headless browser or a live key",
        )


class ICIMS(Adapter):
    key = "icims"
    has_api = False
    signatures = (".icims.com", "icims_content_iframe", "icims.com/jobs")
    slug_regexes = (r"(?:https?://|//|@|\.)([a-z0-9][a-z0-9-]*)\.icims\.com",)
    _GENERIC = {"www", "careers", "jobs", "career", "job", "recruiting", "apply"}

    def extract_slug(self, html, final_url):
        for m in re.finditer(self.slug_regexes[0], (html or "") + "\n" + (final_url or ""), re.I):
            cand = m.group(1).lower()
            if cand not in self._GENERIC:
                return cand
        return None

    def fetch(self, slug, careers_origin=None):
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint="https://%s.icims.com/jobs/search" % (slug or "careers"),
                           note="iCIMS has no public JSON API -> headless browser + location filter")


class Teamtailor(Adapter):
    """Teamtailor exposes a keyless JSON/RSS feed on the *career site* origin
    (``<careers_origin>/jobs.rss``), whether that's ``<slug>.teamtailor.com`` or
    a customer CNAME like ``recrutement.norsys.fr``. The RSS carries structured
    location / department / remote (the JSON Feed at ``/jobs.json`` doesn't)."""
    key = "teamtailor"
    has_api = True
    signatures = ("teamtailor.com", "teamtailor-cdn")
    slug_regexes = (r"([a-z0-9-]+)\.teamtailor\.com",)
    _GENERIC = {"app", "www", "career", "careers", "jobs", "assets", "cdn", "static", "api"}
    _TT_NS = "{https://teamtailor.com/locations}"

    def extract_slug(self, html, final_url):
        for m in re.finditer(self.slug_regexes[0], (html or "") + "\n" + (final_url or ""), re.I):
            cand = m.group(1).lower()
            if cand not in self._GENERIC:
                return cand
        return None

    def _origins(self, slug, careers_origin):
        out = []
        if careers_origin:
            out.append(careers_origin.rstrip("/"))
        if slug and slug not in self._GENERIC:
            tt = "https://%s.teamtailor.com" % slug
            if tt not in out:
                out.append(tt)
        return out

    def fetch(self, slug, careers_origin=None):
        for origin in self._origins(slug, careers_origin):
            url = origin + "/jobs.rss"
            r = get_text(url, retries=0)
            if not r.ok or "<rss" not in r.body[:400].lower():
                continue
            try:
                root = ET.fromstring(r.body.encode("utf-8"))
            except ET.ParseError:
                continue
            jobs = []
            for it in root.iter("item"):
                def g(path):
                    el = it.find(path)
                    return el.text.strip() if el is not None and el.text else None
                ns = self._TT_NS
                # tt:city / tt:country are nested under tt:locations/tt:location
                loc = ", ".join(x for x in (g(".//%scity" % ns), g(".//%scountry" % ns)) if x)
                rs = (g("remoteStatus") or "").lower()
                remote = {"fully": "remote", "temporary": "hybrid"}.get(rs, rs) or None
                jobs.append(Job(g("title"), g("link"), location=loc or None,
                                department=g("%sdepartment" % ns) or g("%srole" % ns),
                                remote=(remote if remote != "none" else None),
                                published_at=_rfc2822(g("pubDate")),
                                description=_strip_html(g("description")) or None,
                                raw={}))
            return FetchResult(self.key, slug, True, jobs, endpoint=url,
                               note="0 offers" if not jobs else None)
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint="https://%s.teamtailor.com/jobs" % (slug or "app"),
                           note="no keyless /jobs.rss at the resolved origin -> headless browser")


class Flatchr(Adapter):
    """FR ATS (Cegid). Career sites are `<slug>.flatchr.io` Next.js SPAs; the
    vacancy list loads client-side and the public REST API needs a key, so we
    only detect + note the slug for a browser pass or a manual `known` block."""
    key = "flatchr"
    has_api = False
    signatures = ("flatchr.io", "flatchr.com")
    slug_regexes = (r"([a-z0-9][a-z0-9-]*)\.flatchr\.io",)
    _GENERIC = {"www", "app", "api", "careers", "career", "jobs", "static", "cdn"}

    def extract_slug(self, html, final_url):
        for m in re.finditer(self.slug_regexes[0], (html or "") + "\n" + (final_url or ""), re.I):
            cand = m.group(1).lower()
            if cand not in self._GENERIC:
                return cand
        return None

    def fetch(self, slug, careers_origin=None):
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint="https://%s.flatchr.io/" % (slug or "") or careers_origin,
                           note="Flatchr vacancy list is client-side; public API needs a key "
                                "-> headless browser or manual `known` block")


class Talentsoft(Adapter):
    """FR / EU enterprise ATS (Cegid Talentsoft). Per-tenant ASP.NET career
    site at `<tenant>.talent-soft.com` (tenant often `<name>-career[s]`). No
    consistent keyless feed -> detect + note the tenant for a browser pass."""
    key = "talentsoft"
    has_api = False
    signatures = ("talent-soft.com", "talentsoft")
    slug_regexes = (r"([a-z0-9][a-z0-9-]*)\.talent-soft\.com",)

    def extract_slug(self, html, final_url):
        raw = _first(self.slug_regexes, (html or "") + "\n" + (final_url or ""))
        return re.sub(r"-careers?$", "", raw) if raw else None

    def fetch(self, slug, careers_origin=None):
        host = ("%s-career.talent-soft.com" % slug) if slug else None
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint=("https://%s/" % host) if host else careers_origin,
                           note="Talentsoft per-tenant site, no keyless feed -> headless browser")


class Custom(Adapter):
    key = "custom"
    has_api = False
    signatures = ()

    def detect(self, html, final_url, headers):
        return False   # only used as an explicit fallback

    def fetch(self, slug, careers_origin=None):
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint=careers_origin,
                           note="bespoke careers page -> dedicated scraper / headless browser")


# --------------------------------------------------------------------------- #
def _rfc2822(v):
    """RFC-2822 date (RSS pubDate) -> ISO8601, pass-through otherwise."""
    if not v:
        return None
    try:
        return parsedate_to_datetime(v).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return v


def _iso_ms(v):
    """Epoch millis -> ISO8601, pass-through otherwise."""
    if isinstance(v, (int, float)) and v > 1_000_000_000:
        try:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return v


# order matters for detection: most specific / least ambiguous first
ADAPTERS = [
    Greenhouse(), Lever(), Ashby(), Recruitee(), Workable(),
    SmartRecruiters(), Personio(), Taleez(), Teamtailor(),
    Flatchr(), Talentsoft(),
    WelcomeToTheJungle(), ICIMS(),
]
BY_KEY = {a.key: a for a in ADAPTERS}
BY_KEY["custom"] = Custom()

API_ATS = sorted(a.key for a in ADAPTERS if a.has_api)
BROWSER_ATS = sorted(a.key for a in ADAPTERS if not a.has_api) + ["custom"]


def fetch_jobs(ats_key, slug, careers_origin=None):
    """Fetch open jobs for a resolved (ats, slug)."""
    adapter = BY_KEY.get(ats_key)
    if adapter is None:
        return FetchResult(ats_key, slug, False, method="none", note="unknown ATS key")
    try:
        res = adapter.fetch(slug, careers_origin=careers_origin)
    except Exception as e:  # noqa: BLE001
        return FetchResult(ats_key, slug, False, note="%s: %s" % (type(e).__name__, e))
    return res
