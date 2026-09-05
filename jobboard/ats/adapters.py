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

from .http import get_json, get_text, post_json


# --------------------------------------------------------------------------- #
#  data types
# --------------------------------------------------------------------------- #
class Job:
    __slots__ = ("title", "location", "department", "contract", "remote", "url", "published_at", "raw")

    def __init__(self, title, url, location=None, department=None, contract=None,
                 remote=None, published_at=None, raw=None):
        self.title = (title or "").strip()
        self.url = url
        self.location = location
        self.department = department
        self.contract = contract
        self.remote = remote
        self.published_at = published_at
        self.raw = raw or {}

    def as_dict(self):
        return {
            "title": self.title,
            "location": self.location,
            "department": self.department,
            "contract": self.contract,
            "remote": self.remote,
            "url": self.url,
            "published_at": self.published_at,
        }


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
    clean = re.sub(r"\b(group|groupe|france|sud|technologies|technology|inc|sa|sas)\b",
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
    signatures = ("boards.greenhouse.io", "job-boards.greenhouse.io",
                  "boards-api.greenhouse.io", "grnhse", "greenhouse.io/embed")
    slug_regexes = (
        r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
        r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)",
        r"grnhse\.io/([a-z0-9_-]+)",
    )

    def fetch(self, slug, careers_origin=None):
        url = "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=false" % slug
        r = get_json(url, retries=1)
        if not r.ok:
            return FetchResult(self.key, slug, False, endpoint=url,
                               note="HTTP %s" % r.status)
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name")
            depts = ", ".join(d.get("name", "") for d in j.get("departments", []) if d)
            jobs.append(Job(j.get("title"), j.get("absolute_url"), location=loc,
                            department=depts or None, published_at=j.get("updated_at"), raw=j))
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
            jobs.append(Job(j.get("text"), j.get("hostedUrl"),
                            location=cat.get("location"),
                            department=cat.get("team") or cat.get("department"),
                            contract=cat.get("commitment"),
                            published_at=_iso_ms(j.get("createdAt")), raw=j))
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
            jobs.append(Job(j.get("title"), j.get("jobUrl") or j.get("applyUrl"),
                            location=j.get("location"),
                            department=j.get("department") or j.get("team"),
                            contract=j.get("employmentType"),
                            remote=j.get("isRemote"),
                            published_at=j.get("publishedAt"), raw=j))
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
                            published_at=j.get("published_at"), raw=j))
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
                               published_at=j.get("releasedDate"), raw=j))
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
                jobs.append(Job(g("name"), jurl, location=g("office"),
                                department=g("department"),
                                contract=g("employmentType"),
                                remote=None, published_at=g("createdAt"), raw={}))
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
        r"taleez\.com/(?:careers|jobs|widget)/([a-z0-9-]+)",
        r"files\.taleez\.com/files/(\d+)/",   # org id fallback
    )

    def fetch(self, slug, careers_origin=None):
        origins = []
        if careers_origin:
            origins.append(careers_origin.rstrip("/"))
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
    key = "teamtailor"
    has_api = False
    signatures = ("teamtailor.com", "teamtailor-cdn")
    slug_regexes = (r"([a-z0-9-]+)\.teamtailor\.com",)

    def fetch(self, slug, careers_origin=None):
        return FetchResult(self.key, slug, False, method="browser",
                           endpoint="https://%s.teamtailor.com/jobs" % slug,
                           note="Teamtailor public feed needs an API token -> headless browser")


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
    SmartRecruiters(), Personio(), Taleez(),
    WelcomeToTheJungle(), ICIMS(), Teamtailor(),
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
