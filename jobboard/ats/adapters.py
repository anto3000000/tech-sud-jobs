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
from urllib.parse import quote

from .http import get_json, get_text, post_json


def _classify(title):
    """Lazy import of jobboard.classify — keeps it an optional dependency for
    every adapter that doesn't need it (only Jobs2Web does, as a cheap gate
    on which postings are worth an extra per-job description GET)."""
    try:
        from jobboard.classify import classify
    except ImportError:
        return True  # can't tell -> don't skip the fetch
    return bool(classify(title))


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


# light France/PACA hint for Workday's `locationsText` — only worth an extra
# per-job detail GET (Workday has no bulk-description endpoint) when the
# listing is plausibly ours; a non-FR posting gets no description (it's
# dropped by the pipeline's PACA filter downstream anyway).
_FR_LOC_RX = re.compile(
    r"\bfrance\b|\b(?:paris|marseille|lyon|toulouse|bordeaux|lille|nantes|nice|strasbourg|"
    r"montpellier|rennes|aix[- ]en[- ]provence|sophia[- ]antipolis|antibes|cannes|avignon|"
    r"valbonne|la\s+ciotat|aubagne|rousset|g[ée]menos|meyreuil|vitrolles|marignane)\b", re.I)


def _first(regexes, text):
    for rx in regexes:
        m = re.search(rx, text, re.I)
        if m:
            return m.group(1)
    return None


def _norm_ascii(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.strip().lower()


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

    # multinational boards (thousands of postings, one detail GET each): ask the
    # API for France only instead of walking the whole world
    FR_ONLY = frozenset(("soprasteria1", "assystem", "alten", "eurofins", "artelia",
                         "wavestone1"))
    # on those boards only PACA postings get the extra per-posting body GET
    _PACA_RX = re.compile(r"aix|marseille|nice\b|sophia|antipolis|toulon|six-fours|biot|valbonne|"
                          r"provence|cannes|antibes|avignon|vitrolles|aubagne|gardanne|cadarache|"
                          r"durance|rousset|sud", re.I)

    def fetch(self, slug, careers_origin=None):
        out, offset = [], 0
        endpoint = "https://api.smartrecruiters.com/v1/companies/%s/postings" % slug
        country = "&country=fr" if slug.lower() in self.FR_ONLY else ""
        while True:
            url = "%s?limit=100&offset=%d%s" % (endpoint, offset, country)
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
                               description=(self._ad_body(slug, jid)
                                            if self._PACA_RX.search(loc_s) else None),
                               raw=j))
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
#  Workday (CXS) — big-corp ATS. No API key: the same JSON the career site's
#  own search box calls is open at <host>/wday/cxs/<tenant>/<site>/jobs.
# --------------------------------------------------------------------------- #
class Workday(Adapter):
    """slug is '<tenant>.wd<N>.myworkdayjobs.com/<site>' (host + site; tenant is
    the host's first label). Career sites almost never live on the company's
    own domain, so this is reached via a `known` override in practice, not
    page-fingerprint discovery."""
    key = "workday"
    has_api = True
    signatures = ("myworkdayjobs.com", "wday/cxs")
    slug_regexes = (
        r"https?://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    )
    _PAGE = 20
    _CAP = 2000  # safety ceiling on total postings walked
    # worldwide boards over Workday's 2000-result window: ask for France only
    # (country facet id from the board's own facets), and only fetch the body
    # of PACA postings.
    FR_FACETS = {
        "thales.wd3.myworkdayjobs.com/Careers":
            {"locationCountry": ["54c5b6971ffb4bf0b116fe7651ec789a"]},
        "ag.wd3.myworkdayjobs.com/Airbus":
            {"locationCountry": ["54c5b6971ffb4bf0b116fe7651ec789a"]},
        "eiffage.wd3.myworkdayjobs.com/Eiffage_Careers":
            {"locationCountry": ["54c5b6971ffb4bf0b116fe7651ec789a"]},
        "accenture.wd103.myworkdayjobs.com/AccentureCareers":
            {"locationCountry": ["54c5b6971ffb4bf0b116fe7651ec789a"]},
    }
    _PACA_RX = re.compile(r"aix|marseille|nice\b|sophia|antipolis|toulon|six-fours|biot|valbonne|"
                          r"provence|cannes|antibes|avignon|vitrolles|aubagne|gardanne|cadarache|"
                          r"durance|rousset|marignane|mougins|grasse|la\s+ciotat|g[ée]menos", re.I)

    def extract_slug(self, html, final_url):
        m = re.search(self.slug_regexes[0], (html or "") + "\n" + (final_url or ""), re.I)
        return "%s/%s" % (m.group(1), m.group(2)) if m else None

    def _detail(self, host, tenant, site, external_path):
        if not external_path:
            return None
        r = get_json("https://%s/wday/cxs/%s/%s%s" % (host, tenant, site, external_path), retries=0)
        if not r.ok:
            return None
        return _body((r.json().get("jobPostingInfo") or {}).get("jobDescription"))

    def fetch(self, slug, careers_origin=None):
        if not slug or "/" not in slug:
            return FetchResult(self.key, slug, False, note="slug must be '<host>/<site>'")
        host, site = slug.split("/", 1)
        tenant = host.split(".")[0]
        url = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
        facets = self.FR_FACETS.get(slug, {})
        detail_rx = self._PACA_RX if facets else _FR_LOC_RX
        jobs, seen, offset, first_total = [], set(), 0, None
        while True:
            r = post_json(url, {"appliedFacets": facets, "limit": self._PAGE,
                                "offset": offset, "searchText": ""}, retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
            data = r.json()
            # some tenants report `total` only on the first page (0 or stale
            # afterwards) and the postings list can wrap around past the true
            # end -> trust the first page's count, dedupe by URL as a backstop.
            if offset == 0:
                first_total = data.get("total", 0)
            postings = data.get("jobPostings") or []
            if not postings:
                break
            new = 0
            for p in postings:
                ext = p.get("externalPath")
                jurl = ("https://%s%s" % (host, ext)) if ext else None
                if not jurl or jurl in seen:
                    continue
                seen.add(jurl)
                new += 1
                # some tenants (Accenture) omit locationsText: city is in bulletFields
                loc = p.get("locationsText") or ", ".join((p.get("bulletFields") or [])[1:])
                desc = self._detail(host, tenant, site, ext) if detail_rx.search(loc) else None
                jobs.append(Job(p.get("title"), jurl, location=loc,
                                published_at=p.get("postedOn"), description=desc, raw=p))
            offset += self._PAGE
            if not new or offset >= (first_total or 0) or offset >= self._CAP:
                break
        return FetchResult(self.key, slug, True, jobs, endpoint=url,
                           note="0 postings" if not jobs else None)


# --------------------------------------------------------------------------- #
#  MACS — Capgemini Group's in-house WordPress jobs plugin, shared verbatim
#  by Capgemini and its sub-brands (Sogeti confirmed same shape). Keyless
#  JSON, and the ?country_code filter does the geo-scoping for us.
# --------------------------------------------------------------------------- #
class Macs(Adapter):
    key = "macs"
    has_api = True
    signatures = ("wp-json/macs/v1", "macs-react-jobs", "cg-jobs-search-frontend")
    slug_regexes = (r"[?&]brand=([A-Za-z0-9_-]+)",)

    def fetch(self, slug, careers_origin=None):
        if not careers_origin:
            return FetchResult(self.key, slug, False, note="MACS needs careers_origin (the WP site)")
        brand = slug or "Capgemini"
        url = "%s/wp-json/macs/v1/jobs?brand=%s&country_code=fr-fr&size=1000" % (
            careers_origin.rstrip("/"), brand)
        r = get_json(url, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
        data = r.json()
        jobs = []
        for j in data.get("data", []):
            jobs.append(Job(j.get("title"), j.get("apply_job_url"),
                            location=j.get("location"),
                            department=j.get("professional_communities") or j.get("sbu") or None,
                            contract=j.get("contract_type"),
                            published_at=_epoch_s(j.get("updated_at")),
                            description=_body(j.get("description")), raw=j))
        return FetchResult(self.key, slug, True, jobs, endpoint=url,
                           note="0 postings" if not jobs else None)


# --------------------------------------------------------------------------- #
#  Jobs2Web — SAP SuccessFactors Recruiting Marketing. No JSON API, but the
#  search results are plain server-rendered HTML
#  (<origin>/search/?q=&locationsearch=<city>&startrow=<n>), so a regex
#  scrape works without a headless browser (confirmed live on CMA CGM).
# --------------------------------------------------------------------------- #
class Jobs2Web(Adapter):
    """slug is the `locationsearch` value (a city name, e.g. 'Marseille')."""
    key = "jobs2web"
    has_api = True
    signatures = ("/platform/js/j2w/", "j2w.search", "jobtitle-link", 'id="searchresults"')

    _ROW_RX = re.compile(
        r'<span class="jobFacility">(?P<id>[^<]*)</span>.*?'
        r'<span class="jobTitle hidden-phone">\s*<a href="(?P<href>[^"]+)"[^>]*>(?P<title>[^<]*)</a>.*?'
        r'<span class="jobLocation">\s*(?P<location>[^<]*?)\s*</span>.*?'
        r'<span class="jobShifttype">(?P<contract>[^<]*)</span>.*?'
        r'<span class="jobDepartment">(?P<dept>[^<]*)</span>',
        re.S)
    _TOTAL_RX = re.compile(r"of <b>(\d+)</b>")
    _DESC_RX = re.compile(r'<span class="jobdescription">(.*?)<p class="job-location">', re.S)
    _PAGE = 25
    _CAP = 500  # safety ceiling on total postings walked for one city

    def _detail(self, origin, href):
        r = get_text(origin + href, retries=0, timeout=15)
        if not r.ok:
            return None
        m = self._DESC_RX.search(r.body)
        return _body(m.group(1)) if m else None

    def fetch(self, slug, careers_origin=None):
        if not careers_origin:
            return FetchResult(self.key, slug, False, note="needs careers_origin (the Jobs2Web site)")
        origin = careers_origin.rstrip("/")
        city = slug or ""
        jobs, seen, offset, first_total = [], set(), 0, None
        while True:
            url = "%s/search/?q=&locationsearch=%s&startrow=%d" % (
                origin, quote(city), offset)
            r = get_text(url, retries=1, timeout=20)
            if not r.ok:
                return FetchResult(self.key, slug, False, endpoint=url, note="HTTP %s" % r.status)
            if offset == 0:
                m = self._TOTAL_RX.search(r.body)
                first_total = int(m.group(1)) if m else 0
            rows = list(self._ROW_RX.finditer(r.body))
            if not rows:
                break
            new = 0
            for m in rows:
                href = m.group("href")
                if href in seen:
                    continue
                seen.add(href)
                new += 1
                title = unescape(m.group("title")).strip()
                # a classify() hit gates the extra per-job GET; every row is
                # still returned (unfiltered) so the pipeline's own PACA/tech
                # filter in build.py stays the single source of truth.
                desc = self._detail(origin, href) if _classify(title) else None
                jobs.append(Job(title, origin + href,
                                location=unescape(m.group("location")).strip(),
                                department=unescape(m.group("dept")).strip() or None,
                                contract=unescape(m.group("contract")).strip() or None,
                                description=desc, raw={"id": m.group("id").strip()}))
            offset += self._PAGE
            if not new or offset >= first_total or offset >= self._CAP:
                break
        return FetchResult(self.key, slug, True, jobs, endpoint="%s/search/" % origin,
                           note="0 postings" if not jobs else None)


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


# --------------------------------------------------------------------------- #
#  Avature (server-rendered "legacy" theme only — Avature's newer "portalpacks"
#  React shell doesn't render job cards into the raw HTML, so tenants that
#  have migrated to it are out of reach for a stdlib-only fetch; keyless GET
#  of SearchJobs/ is tried first and the adapter degrades to `browser` if the
#  markup it expects isn't there). Walks every posting (small/mid boards,
#  a few hundred to ~1000) and keeps only the ones whose department/region
#  names a PACA département — the listing gives region, not city.
#  slug is '<site-origin>/<site-path>', e.g. 'jobs.orano.group/fr_FR/jobs'.
# --------------------------------------------------------------------------- #
class Avature(Adapter):
    key = "avature"
    has_api = True
    signatures = ("avature.net",)
    slug_regexes = (r"([a-z0-9.-]+\.avature\.net/[a-zA-Z0-9_/-]*)",)
    _PAGE = 50
    _PACA_DEPTS = ("bouches-du-rhone", "alpes-maritimes", "var", "vaucluse",
                  "hautes-alpes", "alpes-de-haute-provence",
                  "provence-alpes-cote d'azur", "provence-alpes-cote-d-azur")

    # tenants render the result card as either <article ...> (Orano) or
    # <div ...> (TotalEnergies) with the same class -> split on the opening
    # tag instead of matching a closing tag, which sidesteps the nesting
    # mismatch a naive `.*?</article>` would hit on the <div> variant.
    _CARD_RX = re.compile(r'<(?:article|div)\s+class="article article--result[^"]*"[^>]*>')

    def fetch(self, slug, careers_origin=None):
        if not slug:
            return FetchResult(self.key, slug, False, note="no slug")
        base = "https://%s/SearchJobs/" % slug.rstrip("/")
        out, off = [], 0
        while True:
            r = get_text("%s?jobRecordsPerPage=%d&jobOffset=%d" % (base, self._PAGE, off),
                        retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=base,
                                   note="HTTP %s" % r.status)
            cards = self._CARD_RX.split(r.body)[1:]
            if not cards:
                if off == 0:
                    # migrated to the JS-only "portalpacks" theme -> nothing to walk
                    return FetchResult(self.key, slug, False, method="browser", endpoint=base,
                                       note="no server-rendered job cards (JS-only theme?)")
                break
            for card in cards:
                m = re.search(r'title[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', card, re.S)
                if not m:
                    continue
                loc = re.search(r'list-item-location">([^<]*)</span>', card)
                ref = re.search(r'list-item-ref">([^<]*)</span>', card)
                region = (loc.group(1).split(",")[0].strip() if loc else "")
                if _norm_ascii(region) not in self._PACA_DEPTS:
                    continue
                out.append(Job(unescape(m.group(2)).strip(), m.group(1),
                               location=unescape(loc.group(1)).strip() if loc else None,
                               department=unescape(ref.group(1)).strip() if ref else None,
                               raw={}))
            # jobRecordsPerPage isn't honored by every tenant (some cap the
            # real page size well below it, e.g. 6) -> keep paging by however
            # many cards actually came back until a page is empty.
            off += len(cards)
        return FetchResult(self.key, slug, True, out, endpoint=base,
                           note="0 postings" if not out else None)


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
#  Dassault Systèmes — bespoke careers site (3ds.com) backed by a public
#  Exalead search API (`/apisearch/card_search_api`). No auth; 1 card = 1 job.
#  slug is ignored (single tenant); France-only filter on the card categories.
# --------------------------------------------------------------------------- #
class Dassault3DS(Adapter):
    key = "dassault"
    has_api = True
    signatures = ()
    _EP = "https://www.3ds.com/apisearch/card_search_api"
    _Q = "#all card_content_lang:en (card_content_type=\"career\")"

    def fetch(self, slug, careers_origin=None):
        out, start = [], 0
        while True:
            url = "%s?q=%s&s=desc(card_content_start_datetime)&b=%d&hf=100&output_format=json" % (
                self._EP, quote(self._Q), start)
            r = get_json(url, retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=self._EP,
                                   note="HTTP %s" % r.status)
            data = r.json()
            hits = data.get("hits") or []
            for h in hits:
                m = {}
                for x in h.get("metas", []):
                    m.setdefault(x["name"], x.get("value"))
                cats = [x.get("value") for x in h.get("metas", []) if x["name"] == "meta_cat"]
                if "Country/France" not in cats:
                    continue
                ctype = next((c.split("/", 1)[1] for c in cats if c.startswith("Type/")), None)
                city = (m.get("content_info_2_value") or "").replace("France, ", "")
                out.append(Job(
                    (m.get("content_title") or "").strip(),
                    m.get("content_cta_1_url_id") or m.get("content_cta_1_url"),
                    location=city or None,
                    department=m.get("content_type_display_text"),
                    contract={"Internship": "Stage", "Work Study": "Alternance"}.get(ctype, ctype),
                    published_at=(m.get("content_start_datetime") or "").replace("/", "-").replace(" ", "T") or None,
                    description=_body(m.get("content_summary")), raw={}))
            start += len(hits)
            if not hits or start >= int(data.get("nhits") or 0):
                break
        return FetchResult(self.key, slug, True, out, endpoint=self._EP,
                           note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  Cegid Talentsoft "offre-de-emploi" sites with the keyless RSS handler
#  (`/handlers/offerRss.ashx?LCID=1036[&Rss_Contract=<id>]`, 20 latest items
#  per feed). One feed per contract type is listed on
#  `/offre-de-emploi/tous-les-flux-rss.aspx`; we walk them all and de-dup.
#  slug is the site host (e.g. www.emploi.cea.fr).
# --------------------------------------------------------------------------- #
class TalentsoftRSS(Adapter):
    key = "talentsoft_rss"
    has_api = True
    signatures = ("offerrss.ashx",)

    def fetch(self, slug, careers_origin=None):
        host = (slug or "").replace("https://", "").strip("/")
        base = "https://%s" % host
        page = get_text(base + "/offre-de-emploi/tous-les-flux-rss.aspx", retries=1)
        feeds = ["/handlers/offerRss.ashx?LCID=1036"]
        if page.ok:
            for h in re.findall(r'href="(/handlers/offerRss\.ashx\?LCID=1036[^"]*)"', page.body):
                h = unescape(h)
                if h not in feeds:
                    feeds.append(h)
        seen, out = set(), []
        for f in feeds:
            r = get_text(base + f, retries=1)
            if not r.ok:
                continue
            try:
                root = ET.fromstring(r.body.encode("utf-8"))
            except ET.ParseError:
                continue
            for it in root.iter("item"):
                link = (it.findtext("link") or "").strip()
                m = re.search(r"idOffre=(\d+)", link)
                key = m.group(1) if m else link
                if not link or key in seen:
                    continue
                seen.add(key)
                desc = it.findtext("description") or ""
                contract = _first([r"Contrat\s*:\s*</b>\s*([^<\n]+)"], desc)
                domain = _first([r"Domaine\s*:\s*</b>\s*([^<\n]+)"], desc)
                title = re.sub(r"^\d{4}-\d+\s*-\s*", "", (it.findtext("title") or "").strip())
                city = _first([r"Ville\s*:\s*</b>\s*([^<\n]+)"], desc)
                out.append(Job(title, link, location=(city or it.findtext("category") or "").strip() or None,
                               department=(domain or "").strip() or None,
                               contract=(contract or "").strip() or None,
                               published_at=_rfc2822(it.findtext("pubDate")),
                               description=_strip_html(desc), raw={}))
        return FetchResult(self.key, slug, bool(out), out, endpoint=base + feeds[0],
                           note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  Deloitte France — careers portal (deloitte.com/fr/fr/careers/content/job/
#  results.html) is a client-side app over an AWS API Gateway endpoint that
#  takes the public x-api-key shipped in its own JS (same key every visitor
#  uses). POST {size, from, fields[]} -> {total, results[]}. slug ignored.
# --------------------------------------------------------------------------- #
class DeloitteFR(Adapter):
    key = "deloittefr"
    has_api = True
    signatures = ("f6nv82mofd.execute-api",)
    _EP = "https://f6nv82mofd.execute-api.eu-west-1.amazonaws.com/prod/offres_v2"
    _KEY = "JKT2pdDzG35s3MoPXwmy3TjLcCALbuj9SP6bTPt1"
    _FIELDS = ["id", "jobname", "city_name", "country", "activity_title", "contract_type",
               "link", "job_category_title", "last_posting_date", "description",
               "additional_description", "remote_type"]

    def fetch(self, slug, careers_origin=None):
        out, start, total = [], 0, None
        while total is None or start < total:
            r = post_json(self._EP, {"size": 100, "from": start, "hideScore": True,
                                     "fields": self._FIELDS},
                          headers={"x-api-key": self._KEY}, retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=self._EP,
                                   note="HTTP %s" % r.status)
            data = r.json()
            total = int(data.get("total") or 0)
            res = data.get("results") or []
            for j in res:
                link = (j.get("link") or "").replace("/apply", "")
                city = re.sub(r"\s+\d{2}$", "", (j.get("city_name") or "").strip())
                out.append(Job((j.get("jobname") or "").strip(), link,
                               location=city.title() or None,
                               department=j.get("job_category_title") or j.get("activity_title"),
                               contract=j.get("contract_type"),
                               remote=j.get("remote_type"),
                               published_at=j.get("last_posting_date"),
                               description=_body(j.get("description"), j.get("additional_description")),
                               raw={}))
            start += len(res)
            if not res:
                break
        return FetchResult(self.key, slug, True, out, endpoint=self._EP,
                           note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  SYSTRA — WordPress CPT `systra_jobs` on the public REST API, filtered to the
#  France country term (14604). The CPT carries no city field: the location is
#  read from the title ("… Marseille (13)") or the first lines of the body.
# --------------------------------------------------------------------------- #
class SystraWP(Adapter):
    key = "systrawp"
    has_api = True
    _BASE = "https://www.systra.com/wp-json/wp/v2"
    _FR = 14604
    _CITY_RX = re.compile(
        r"(marseille|aix[- ]en[- ]provence|nice|toulon|avignon|sophia[- ]antipolis|cannes|"
        r"antibes|la ciotat|vitrolles|aubagne|paris|lyon|lille|bordeaux|toulouse|nantes|"
        r"rennes|strasbourg|montpellier|grenoble|n[iî]mes)", re.I)

    def _terms(self, tax):
        r = get_json("%s/%s/?per_page=100&lang=fr" % (self._BASE, tax), retries=1)
        return {t["id"]: t["name"] for t in r.json()} if r.ok else {}

    def fetch(self, slug, careers_origin=None):
        contracts, domains = self._terms("systra_job_contract"), self._terms("systra_job_domain")
        out, page = [], 1
        while True:
            url = ("%s/systra_jobs/?per_page=100&page=%d&lang=fr&systra_job_country=%d"
                   "&_fields=id,link,date,title,content,systra_job_contract,systra_job_domain"
                   % (self._BASE, page, self._FR))
            r = get_json(url, retries=1)
            if not r.ok:
                if page > 1 and r.status == 400:   # past the last page
                    break
                return FetchResult(self.key, slug, bool(out), out, endpoint=url, note="HTTP %s" % r.status)
            rows = r.json()
            for j in rows:
                title = unescape(((j.get("title") or {}).get("rendered") or "")).strip()
                body = _body((j.get("content") or {}).get("rendered"))
                m = self._CITY_RX.search(title) or self._CITY_RX.search((body or "")[:1200])
                cid = (j.get("systra_job_contract") or [None])[0]
                did = (j.get("systra_job_domain") or [None])[0]
                out.append(Job(title, j.get("link"), location=m.group(1).title() if m else "France",
                               department=domains.get(did), contract=contracts.get(cid),
                               published_at=(j.get("date") or None) and j["date"] + "Z",
                               description=body, raw={}))
            if len(rows) < 100:
                break
            page += 1
        return FetchResult(self.key, slug, True, out, endpoint=self._BASE + "/systra_jobs",
                           note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  INRAE — jobs.inrae.fr is Drupal + Algolia InstantSearch; the search-only
#  key ships in the page. French-language nodes, PACA region facet. The teaser
#  carries "<postcode> <city>". slug ignored.
# --------------------------------------------------------------------------- #
class InraeAlgolia(Adapter):
    key = "inrae"
    has_api = True
    _APP, _KEY, _IDX = "DVUTVWXJFU", "1e2d2d60b4de29e857a2d1be26bb2fcd", "inrae_prod_created_date_desc"

    def fetch(self, slug, careers_origin=None):
        ep = "https://%s-dsn.algolia.net/1/indexes/%s/query" % (self._APP.lower(), self._IDX)
        out, page = [], 0
        while True:
            params = ("query=&hitsPerPage=100&page=%d&filters=%s" % (
                page, quote("search_api_language:fr AND field_region_value:\"Provence-Alpes-Côte d'Azur\"")))
            r = post_json(ep, {"params": params},
                          headers={"X-Algolia-Application-Id": self._APP, "X-Algolia-API-Key": self._KEY},
                          retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=ep, note="HTTP %s" % r.status)
            data = r.json()
            for h in data.get("hits", []):
                teaser = _strip_html(h.get("rendered_item_teaser_jobs") or "")
                m = re.search(r"\b\d{5}\s+([^\n]+)", teaser)
                agreement = h.get("field_offer_agreement")
                if agreement in ("CONCOURS", "MOBILITÉ", "CHAIRE"):
                    continue   # civil-servant exams / internal transfers, not open applications
                out.append(Job(h.get("title"), "https://jobs.inrae.fr" + (h.get("url") or ""),
                               location=(m.group(1).strip() if m else None) or h.get("field_related_center_name"),
                               department=h.get("field_related_departments_name"),
                               contract={"Mission temporaire": "CDD", "Postdoc": "Post-doctorat",
                                         "Thèse": "Thèse"}.get(agreement, agreement),
                               published_at=_epoch_s(h.get("created")),
                               description=teaser or None, raw={}))
            page += 1
            if page >= int(data.get("nbPages") or 0):
                break
        return FetchResult(self.key, slug, True, out, endpoint=ep, note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  iCIMS career portals (Expleo FR, …) — the classic server-rendered search
#  (`/jobs/search?ss=1&in_iframe=1&pr=<page>`) is plain HTML with one card per
#  job (title, "FR-13-Marseille" location, type, snippet). slug is the portal
#  host, e.g. expleo-jobs-fr-fr.icims.com.
# --------------------------------------------------------------------------- #
class IcimsPortal(Adapter):
    key = "icims_portal"
    has_api = True
    signatures = ("icims_jobscardlist", "icims_jobcarditem")

    def fetch(self, slug, careers_origin=None):
        host = (slug or "").replace("https://", "").strip("/")
        base = "https://%s" % host
        out, seen, page, pages = [], set(), 0, None
        while pages is None or page < pages:
            r = get_text("%s/jobs/search?ss=1&in_iframe=1&pr=%d" % (base, page), retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=base, note="HTTP %s" % r.status)
            if pages is None:
                m = re.search(r"page\s+\d+\s+(?:de|of)\s+(\d+)", r.body, re.I)
                pages = int(m.group(1)) if m else 1
            for card in re.split(r'<li class="iCIMS_JobCardItem">', r.body)[1:]:
                a = re.search(r'href="([^"]*/jobs/(\d+)/[^"]*)"[^>]*title="[^"]*"', card)
                if not a or a.group(2) in seen:
                    continue
                seen.add(a.group(2))
                title = _strip_html(re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S).group(1)) if "<h3" in card else ""
                desc = re.search(r'class="col-xs-12 description">(.*?)</div>', card, re.S)
                loc = re.search(r"Job Locations.*?<dd[^>]*>(.*?)</dd>", card, re.S)
                ctr = re.search(r"Type d.emploi</dt>\s*<dd[^>]*><span[^>]*>\s*([^<]+?)\s*</span>", card, re.S)
                dep = re.search(r"M[ée]tiers</dt>\s*<dd[^>]*><span[^>]*>\s*([^<]+?)\s*</span>", card, re.S)
                # multi-site postings: "FR-31-Toulouse | FR-13-Vitrolles" -> "Toulouse | Vitrolles"
                parts = [re.sub(r"^[A-Z]{2}-(?:\w{1,3}-)?", "", unescape(x).strip())
                         for blk in re.findall(r"<span[^>]*>\s*([^<]+?)\s*</span>", loc.group(1))
                         for x in blk.split("|")] if loc else []
                city = " | ".join(x for x in parts if x) or None
                out.append(Job(title, a.group(1).replace("?in_iframe=1", ""), location=city,
                               department=unescape(dep.group(1)) if dep else None,
                               contract=unescape(ctr.group(1)) if ctr else None,
                               description=_strip_html(desc.group(1)) if desc else None, raw={}))
            page += 1
        return FetchResult(self.key, slug, True, out, endpoint=base, note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  VINCI Energies — WordPress archive `/job-offer/` (REST is locked, ~1900
#  worldwide postings over 190 pages). The keyword field `job_s` matches the
#  address, so we search a list of PACA towns and de-dup. slug ignored.
# --------------------------------------------------------------------------- #
class VinciEnergies(Adapter):
    key = "vincienergies"
    has_api = True
    _BASE = "https://www.vinci-energies.com/job-offer/"
    _CONTRACTS = {"Contrat à durée indéterminée": "CDI", "Contrat à durée déterminée": "CDD",
                  "Convention de stage": "Stage", "Contrat d'apprentissage": "Alternance",
                  "Contrat de professionnalisation": "Alternance"}
    _TOWNS = ("Marseille", "Aix-en-Provence", "Aubagne", "Vitrolles", "Marignane", "Gardanne",
              "Toulon", "La Ciotat", "Nice", "Sophia Antipolis", "Cannes", "Antibes", "Avignon",
              "Fos-sur-Mer", "Martigues", "Salon-de-Provence", "Pertuis", "Manosque",
              "Provence-Alpes-Côte d'Azur")

    def fetch(self, slug, careers_origin=None):
        out, seen = [], set()
        for town in self._TOWNS:
            page = 1
            while page <= 10:
                url = ("%s%s?job_s=%s" % (self._BASE, "page/%d/" % page if page > 1 else "", quote(town)))
                r = get_text(url, retries=1)
                if not r.ok:
                    break
                res = r.body.split('class="search-results-list"', 1)
                cards = re.split(r'<li class="item[^"]*">', res[1])[1:] if len(res) > 1 else []
                for c in cards:
                    a = re.search(r'href="([^"]+)"[^>]*class="row-fake-link"', c)
                    t = re.search(r'<h2 class="title[^>]*>(.*?)</h2>', c, re.S)
                    if not a or not t or a.group(1) in seen:
                        continue
                    seen.add(a.group(1))
                    c2 = re.sub(r"<svg.*?</svg>", "", c, flags=re.S)
                    loc = re.search(r'class="location">\s*(?:<span class="icon">\s*</span>)?\s*([^<]+)', c2)
                    cat = re.search(r'section-label">([^<]+)', c2)
                    ctr = re.search(r'additional-infos__status">\s*(?:<span class="icon">\s*</span>)?\s*([^<]+)', c2)
                    out.append(Job(_strip_html(t.group(1)), a.group(1),
                                   location=unescape(loc.group(1)).strip() if loc else town,
                                   department=unescape(cat.group(1)).strip().title() if cat else None,
                                   contract=self._CONTRACTS.get(unescape(ctr.group(1)).strip(), unescape(ctr.group(1)).strip()) if ctr else None,
                                   raw={}))
                if not cards or ("/page/%d/" % (page + 1)) not in r.body:
                    break
                page += 1
        return FetchResult(self.key, slug, True, out, endpoint=self._BASE, note="0 postings" if not out else None)


# --------------------------------------------------------------------------- #
#  Amazon Jobs — public search.json (country=FRA). slug ignored.
# --------------------------------------------------------------------------- #
class AmazonJobs(Adapter):
    key = "amazonjobs"
    has_api = True
    signatures = ("amazon.jobs",)

    def fetch(self, slug, careers_origin=None):
        out, offset, ep = [], 0, "https://www.amazon.jobs/en/search.json"
        while offset < 1000:
            url = "%s?country=FRA&result_limit=100&offset=%d&sort=recent" % (ep, offset)
            r = get_json(url, retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=ep, note="HTTP %s" % r.status)
            data = r.json()
            jobs = data.get("jobs") or []
            for j in jobs:
                out.append(Job(j.get("title"), "https://www.amazon.jobs" + (j.get("job_path") or ""),
                               location=j.get("normalized_location") or j.get("location"),
                               department=j.get("job_category"),
                               published_at=_amazon_date(j.get("posted_date")),
                               description=_body(j.get("description"), j.get("basic_qualifications"),
                                                 heads=["", "Qualifications"]), raw={}))
            offset += len(jobs)
            if not jobs or offset >= int(data.get("hits") or 0):
                break
        return FetchResult(self.key, slug, True, out, endpoint=ep, note="0 postings" if not out else None)


def _amazon_date(v):
    try:
        return datetime.strptime(v, "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
def _rfc2822(v):
    """RFC-2822 date (RSS pubDate) -> ISO8601, pass-through otherwise."""
    if not v:
        return None
    try:
        return parsedate_to_datetime(v).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return v


def _epoch_s(v):
    """Epoch seconds (int or numeric string) -> ISO8601, pass-through otherwise."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return v
    if v > 1_000_000_000:
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _iso_ms(v):
    """Epoch millis -> ISO8601, pass-through otherwise."""
    if isinstance(v, (int, float)) and v > 1_000_000_000:
        try:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return v


# order matters for detection: most specific / least ambiguous first
# --------------------------------------------------------------------------- #
#  Phenom People career sites (orange.jobs). Keyless POST /widgets
#  (ddoKey=refineSearch) returns every posting with structured city/country;
#  we ask for France only. slug is the site host (orange.jobs).
# --------------------------------------------------------------------------- #
class PhenomWidgets(Adapter):
    key = "phenom"
    has_api = True
    signatures = ("phenompeople.com", "phenom-footer")

    def _query(self, slug, off, size):
        return {"lang": "fr_fr", "deviceType": "desktop", "country": "fr",
                "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "",
                "subsearch": "", "from": off, "jobs": True, "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"],
                "pageType": "landingPage", "size": size, "clearAll": False,
                "jdsource": "facets", "isSliderEnable": False, "pageId": "page11",
                "siteType": "external", "location": "", "keywords": "", "global": True,
                "selected_fields": {"country": ["FRANCE"]},
                "sort": {"order": "desc", "field": "postedDate"}, "locationData": {}}

    def fetch(self, slug, careers_origin=None):
        url = "https://%s/widgets" % slug
        out, off = [], 0
        while True:
            r = post_json(url, self._query(slug, off, 100), retries=1)
            if not r.ok:
                return FetchResult(self.key, slug, bool(out), out, endpoint=url,
                                   note="HTTP %s" % r.status)
            data = (r.json().get("refineSearch") or {})
            jobs = (data.get("data") or {}).get("jobs") or []
            for j in jobs:
                seq = j.get("jobSeqNo")
                city = (j.get("city") or "")
                city = "" if "SPÉCIFIÉ" in city.upper() else city.title()
                out.append(Job(
                    j.get("title"), "https://%s/fr/fr/job/%s" % (slug, seq) if seq else None,
                    location=("%s, France" % city) if city else None,
                    department=j.get("category"), contract=j.get("contractType"),
                    published_at=j.get("postedDate"),
                    description=_body(j.get("descriptionTeaser")), raw={}))
            off += len(jobs)
            if not jobs or off >= int(data.get("totalHits") or 0):
                break
        return FetchResult(self.key, slug, True, out, endpoint=url,
                           note="0 postings" if not out else None)


ADAPTERS = [
    Greenhouse(), Lever(), Ashby(), Recruitee(), Workable(),
    SmartRecruiters(), Personio(), Taleez(), Teamtailor(),
    Flatchr(), Talentsoft(), Workday(), Macs(), Jobs2Web(),
    WelcomeToTheJungle(), ICIMS(), Dassault3DS(), AmazonJobs(), TalentsoftRSS(),
    DeloitteFR(), SystraWP(), InraeAlgolia(), IcimsPortal(), VinciEnergies(), PhenomWidgets(), Avature(),
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
