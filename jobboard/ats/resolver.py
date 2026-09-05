"""Slug / ATS resolver.

Reco #2: instead of *guessing* an ``ats`` + ``slug`` per company, discover them.

Strategy, cheapest signal first:
  1. Try every ATS' own hosted board with a few slug variants
     (``jobs.lever.co/<slug>``, ``<slug>.recruitee.com`` ...). A 200 there is
     ground truth and also yields the canonical slug.
  2. Fetch the company careers page (from ``domain`` + common path guesses),
     read the *raw* HTML and look for an ATS fingerprint / embed URL.
  3. Fall back to the declared ATS, or ``custom``.

Everything is stdlib + serial-with-threads; no external deps.
"""
import concurrent.futures
import re
import unicodedata

from .adapters import ADAPTERS, BY_KEY, slug_variants
from .http import get_text

# Workable & SmartRecruiters return HTTP 200 for *any* existing account
# (and generic slugs like "arm" / "amadeus" are taken by unrelated orgs),
# so blind slug probing is unsafe -> they are detected via page fingerprint
# or an explicit `known` override only, never guessed here.
_LENIENT_API = set()

CAREERS_PATHS = [
    "/careers", "/career", "/careers/", "/jobs", "/jobs/", "/join-us", "/join",
    "/carrieres", "/carriere", "/nos-offres", "/nous-rejoindre", "/recrutement",
    "/fr/careers", "/en/careers", "/company/careers", "/about/careers",
    "/company/jobs", "/who-we-are/careers", "/life", "/team",
]
CAREERS_SUBDOMAINS = ["careers.", "jobs.", "career.", "carriere.", "carrieres.", "work.", "join."]

# ATS with a public jobs API we can probe directly with slug guesses.
# The direct-probe pass calls the adapter's real API (not a marketing URL),
# so a hit means the board genuinely exists.
DIRECT_API_ATS = ["greenhouse", "lever", "ashby", "recruitee", "personio"]


def _norm_domain(domain):
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].strip()
    return d or None


def guess_domains(name, domain):
    """Best-effort domain candidates when none is supplied."""
    if domain:
        return [_norm_domain(domain)]
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "", base.lower())
    if not base:
        return []
    return ["%s.com" % base, "%s.fr" % base, "%s.io" % base, "%s.ai" % base]


def candidate_pages(name, domain):
    urls = []
    for d in guess_domains(name, domain):
        if not d:
            continue
        urls.append("https://%s/" % d)
        for sub in CAREERS_SUBDOMAINS:
            urls.append("https://%s%s/" % (sub, d))
        for p in CAREERS_PATHS:
            urls.append("https://%s%s" % (d, p))
    # de-dupe, keep order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _origin(url):
    m = re.match(r"(https?://[^/]+)", url or "")
    return m.group(1) if m else None


def _detect_on_page(html, final_url, headers):
    """Return (ats_key, slug or None) for the first adapter that fingerprints."""
    for ad in ADAPTERS:
        if ad.detect(html, final_url, headers):
            return ad.key, ad.extract_slug(html, final_url)
    return None, None


def direct_probe(name, declared_ats, declared_slug):
    """Pass 1 — call each ATS' real jobs API with a few slug variants.

    A 200 from the API (with >=1 job for the lenient ones) is ground truth
    and hands us the canonical slug.
    """
    variants = slug_variants(name, declared_slug)
    order = ([declared_ats] if declared_ats in DIRECT_API_ATS else []) + \
            [k for k in DIRECT_API_ATS if k != declared_ats]
    for ats in order:
        adapter = BY_KEY[ats]
        for s in variants:
            if not s:
                continue
            res = adapter.fetch(s)
            if not res.ok:
                continue
            if ats in _LENIENT_API and not res.jobs:
                continue  # API 200s for unknown orgs -> require real jobs
            return {
                "ats": ats, "slug": res.slug or s,
                "careers_url": res.endpoint,
                "careers_origin": _origin(res.endpoint),
                "via": "direct-probe", "evidence": res.endpoint,
                "job_count": len(res.jobs),
            }
    return None


def _looks_empty(html):
    low = (html or "").lower()
    return any(x in low for x in (
        "page not found", "404 not found", "not_found",
        "no longer accepting", "aucune offre",
    )) and len(low) < 4000


def page_probe(name, domain):
    """Pass 2 — fetch careers pages, fingerprint the ATS embed."""
    for url in candidate_pages(name, domain):
        r = get_text(url, retries=0, timeout=15)
        if not r.ok or len(r.body) < 500:
            continue
        ats, slug = _detect_on_page(r.body, r.url, r.headers)
        if ats:
            return {
                "ats": ats, "slug": slug, "careers_url": r.url,
                "careers_origin": _origin(r.url), "via": "page-fingerprint",
                "evidence": url,
            }
    return None


def resolve_company(company, do_direct=True, do_page=True):
    """company: dict with keys name, [domain], [ats], [slug].

    Returns a resolution dict (never raises).
    """
    name = company["name"]
    domain = _norm_domain(company.get("domain"))
    declared_ats = (company.get("ats") or "").strip().lower() or None
    declared_slug = (company.get("slug") or "").strip().lower() or None

    result = {
        "name": name,
        "domain": domain,
        "declared_ats": declared_ats,
        "declared_slug": declared_slug,
        "resolved_ats": None,
        "resolved_slug": None,
        "careers_url": None,
        "careers_origin": None,
        "method": None,          # api | browser | none
        "resolved_via": None,    # direct-probe | page-fingerprint | declared | unresolved
        "evidence": None,
        "confidence": "none",
        "needs_review": False,
    }

    # 0. explicit human-verified override wins over any probing
    known = company.get("known")
    if known and known.get("ats"):
        kats = known["ats"].strip().lower()
        adapter = BY_KEY.get(kats)
        result.update(
            resolved_ats=kats,
            resolved_slug=(known.get("slug") or declared_slug),
            careers_url=known.get("careers_url"),
            careers_origin=_origin(known.get("careers_url") or ""),
            resolved_via="known",
            evidence=known.get("note") or "verified manually",
            method="api" if (adapter and adapter.has_api and kats != "custom") else "browser",
            confidence="verified",
        )
        return result

    hit = None
    if do_direct:
        try:
            hit = direct_probe(name, declared_ats, declared_slug)
        except Exception as e:  # noqa: BLE001
            result["evidence"] = "direct-probe error: %s" % e
    if hit is None and do_page:
        try:
            hit = page_probe(name, domain)
        except Exception as e:  # noqa: BLE001
            result["evidence"] = "page-probe error: %s" % e

    if hit:
        via = hit["via"]
        conf = "high" if via == "direct-probe" else "medium"
        needs_review = False
        # A blind hosted-board hit on a short, dictionary-word slug that
        # contradicts the declared ATS is a likely name collision with an
        # unrelated public board -> flag it for a human glance.
        if via == "direct-probe":
            slug = (hit["slug"] or "")
            collision_prone = len(slug) <= 8 or "-" not in slug
            if declared_ats and declared_ats not in ("custom", hit["ats"]) and collision_prone:
                conf, needs_review, via = "medium", True, "direct-probe-unverified"
        result.update(
            resolved_ats=hit["ats"],
            resolved_slug=hit["slug"],
            careers_url=hit["careers_url"],
            careers_origin=hit["careers_origin"],
            resolved_via=via,
            evidence=hit["evidence"],
            method="api" if BY_KEY[hit["ats"]].has_api else "browser",
            confidence=conf,
            needs_review=needs_review,
        )
        return result

    # nothing detected online -> keep declared as a hypothesis
    if declared_ats and declared_ats in BY_KEY:
        result.update(
            resolved_ats=declared_ats if declared_ats != "custom" else "custom",
            resolved_slug=declared_slug,
            resolved_via="declared",
            method="api" if BY_KEY.get(declared_ats) and BY_KEY[declared_ats].has_api else "browser",
            confidence="low",
        )
    else:
        result.update(resolved_ats="custom", resolved_via="unresolved",
                      method="browser", confidence="none")
    return result


def resolve_all(companies, workers=8, **kw):
    out = [None] * len(companies)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(resolve_company, c, **kw): i for i, c in enumerate(companies)}
        for fut in concurrent.futures.as_completed(futs):
            out[futs[fut]] = fut.result()
    return out
