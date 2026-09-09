#!/usr/bin/env python3
"""Layer 4 — company profiles.

Reads the built feed (jobboard/site/jobs.json) plus whatever enrichment is on
disk and writes ONE row per hiring company to jobboard/site/companies.json:

    * aggregates computed from that company's live offers
      (open roles, métier / contract / city split, consolidated tech stack,
       remote posture, experience mix, salary samples, hiring rhythm)
    * a company profile distilled from the Welcome to the Jungle detail cache
      (description, sector, headcount, founding year, HQ, gender split, socials,
       cover image) — WTTJ-sourced companies only
    * ecosystem membership + resolved ATS, matched by domain / name against the
      Layer-2 directories (data/companies.all.json, companies.resolved.json)

render_pages.py turns each row into site/entreprise/<slug>.html.

    python3 jobboard/build_companies.py

Everything it reads is optional except site/jobs.json: run build.py first.
"""
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SITE = os.path.join(HERE, "site")
FEED = os.path.join(SITE, "jobs.json")
WTTJ_CACHE = os.path.join(DATA, "cache", "wttj")
OUT = os.path.join(SITE, "companies.json")

# stack tokens that aren't a real signal of what a company builds
STACK_DENY = {"claude", "excel", "notion", "slack", "google-ads", "google-analytics",
              "confluence", "jira", "office", "microsoft-office", "powerpoint", "word",
              "sonar", "windows", "gmail", "outlook", "teams"}

# data/companies.*.json `source` -> human ecosystem label (curated is not one)
ECOSYSTEM = {
    "frenchtech-aix-marseille": "French Tech Aix-Marseille",
    "frenchtech-cote-dazur": "French Tech Côte d'Azur",
    "telecom-valley": "Telecom Valley",
    "aktantis": "Aktantis (deeptech / ex-Pôle SCS)",
    "medinsoft": "Medinsoft",
}

EXP_ORDER = ["Débutant", "< 1 an", "1–2 ans", "2–5 ans", "5–7 ans", "7–10 ans", "> 10 ans"]

# coarse "kind of employer" — WTTJ has no funding/status field, so this is a
# headcount heuristic plus a hand list of the ESN / conseil houses that dominate
# the PACA feed (they'd otherwise all read "Grand groupe").
ESN_RX = re.compile(
    r"\b(sopra ?steria|capgemini|atos|groupe sii|\bsii\b|inetum|akka|alten|assystem|"
    r"devoteam|\bcgi\b|accenture|sogeti|expleo|segula|\bausy\b|neurones|micropole|"
    r"keyrus|meritis|exalt|talan|wavestone|umanis|hardis|smile|niji|econocom|"
    r"cs group|\bcs\b|orange business|sword|aubay|astek|ausy|nexen|magellan|mc2i|"
    r"scalian|akkodis|amaris|ekium|bertin)\b", re.I)


def company_type(name, headcount):
    if ESN_RX.search(name or ""):
        return "ESN / conseil"
    if not headcount:
        return None
    if headcount < 30:
        return "Startup"
    if headcount < 250:
        return "Scale-up / PME"
    if headcount < 5000:
        return "ETI"
    return "Grand groupe"


def _slug(s):
    """compact, punctuation-free key for matching (matches build.py._slug)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def page_slug(s):
    """dash-slug for the URL — MUST match render_pages.slugify."""
    s = str(s or "").lower()
    for a, b in (("c++", "cpp"), ("c#", "csharp"), (".net", "dotnet"), ("f#", "fsharp"),
                 ("node.js", "nodejs"), ("next.js", "nextjs")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)


def strip_html(s, cap=600):
    s = re.sub(r"<(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    from html import unescape
    s = unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    if len(s) > cap:
        s = s[:cap].rsplit(" ", 1)[0].rstrip(" ,;.") + "…"
    return s or None


# --------------------------------------------------------------------------- #
#  WTTJ org profile                                                           #
# --------------------------------------------------------------------------- #
def wttj_org_from_url(url):
    """.../fr/companies/<org>/jobs/<slug> -> <org>"""
    m = re.search(r"/companies/([^/]+)/jobs/", url or "")
    return m.group(1) if m else None


def distill_org(org):
    if not org:
        return None
    hq = org.get("headquarter") or {}
    eq = org.get("equality_indexes") or {}
    socials = {}
    for key, field in (("website", "media_website_url"), ("linkedin", "media_linkedin"),
                       ("twitter", "media_twitter"), ("instagram", "media_instagram"),
                       ("youtube", "media_youtube"), ("facebook", "media_facebook")):
        v = (org.get(field) or "").strip()
        if not v:
            continue
        if key == "website" and not v.startswith("http"):
            v = "https://" + v.lstrip("/")
        if key == "linkedin" and not v.startswith("http"):
            v = "https://www.linkedin.com/company/" + v.strip("/")
        if key == "twitter" and not v.startswith("http"):
            v = "https://twitter.com/" + v.lstrip("@")
        if key == "instagram" and not v.startswith("http"):
            v = "https://www.instagram.com/" + v.strip("/")
        if key == "youtube" and not v.startswith("http"):
            v = "https://www.youtube.com/" + v.strip("/")
        if key == "facebook" and not v.startswith("http"):
            v = "https://www.facebook.com/" + v.strip("/")
        socials[key] = v
    sectors = [s.get("name") for s in (org.get("sectors") or []) if s.get("name")]
    cover = (((org.get("cover_image") or {}).get("small") or {}).get("url")
             or (org.get("cover_image") or {}).get("url"))
    nb = org.get("nb_employees")
    try:
        nb = int(nb) if nb not in (None, "", 0) else None
    except (TypeError, ValueError):
        nb = None
    wo = org.get("website_organization") or {}
    # WTTJ carries a localized description; prefer the French one (present for
    # ~99% of orgs), fall back to the generic `description` (often English).
    fr_desc = (wo.get("i18n_descriptions") or {}).get("fr")
    wslug = wo.get("slug")
    return {
        "name": org.get("name"),
        "wttj_slug": wslug,
        "wttj_url": ("https://www.welcometothejungle.com/fr/companies/%s" % wslug)
        if wslug else None,
        "description": strip_html(fr_desc or org.get("description")),
        "sectors": sectors,
        "industry": org.get("industry") or None,
        "headcount": nb,
        "founded": org.get("creation_year") or None,
        "avg_age": org.get("average_age") or None,
        "hq_city": hq.get("city") or None,
        "hq_country": hq.get("country_code") or None,
        "parity_women": org.get("parity_women"),
        "parity_men": org.get("parity_men"),
        "equality_index": eq.get("equality_index"),
        "socials": socials,
        "cover_image": cover,
        "logo": ((org.get("logo") or {}).get("thumb") or {}).get("url")
        or (org.get("logo") or {}).get("url"),
    }


def load_wttj_org_for(job):
    """Read the enrich cache file for one WTTJ job and distill its organization."""
    oid = (job.get("id") or "").split(":", 1)[-1]
    p = os.path.join(WTTJ_CACHE, "%s.json" % oid)
    if not oid or not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    j = d.get("job", d) or {}
    return distill_org(j.get("organization"))


# --------------------------------------------------------------------------- #
#  directory match (ecosystems + resolved ATS)                                #
# --------------------------------------------------------------------------- #
def load_directories():
    by_name, by_domain = {}, {}
    p = os.path.join(DATA, "companies.all.json")
    if os.path.exists(p):
        for c in json.load(open(p, encoding="utf-8")):
            by_name.setdefault(_slug(c.get("name")), []).append(c)
            if c.get("domain"):
                by_domain.setdefault(c["domain"].lower().lstrip("www."), []).append(c)
    resolved = {}
    p = os.path.join(HERE, "companies.resolved.json")
    if os.path.exists(p):
        for c in json.load(open(p, encoding="utf-8")):
            resolved[_slug(c.get("name"))] = c
            if c.get("domain"):
                resolved.setdefault(c["domain"].lower().lstrip("www."), c)
    return by_name, by_domain, resolved


def directory_extras(name, domain, dirs):
    by_name, by_domain, resolved = dirs
    rows = []
    if domain:
        rows = by_domain.get(domain.lower().lstrip("www."), [])
    if not rows:
        rows = by_name.get(_slug(name), [])
    ecosystems, tags, ats, city, dept = [], [], None, None, None
    for c in rows:
        lab = ECOSYSTEM.get(c.get("source"))
        if lab and lab not in ecosystems:
            ecosystems.append(lab)
        for t in (c.get("tags") or []):
            if t not in tags:
                tags.append(t)
        ats = ats or (c.get("known") or {}).get("ats") or c.get("ats")
        city = city or c.get("city")
        dept = dept or c.get("dept")
    r = resolved.get(_slug(name)) or (resolved.get(domain.lower().lstrip("www."))
                                      if domain else None)
    careers_url = None
    if r:
        ats = (r.get("resolved_ats") if r.get("method") == "api" else None) or ats
        careers_url = r.get("careers_url")
    return {
        "ecosystems": ecosystems,
        "tags": tags[:8],
        "ats": ats,
        "careers_url": careers_url,
        "dir_city": city,
        "dept": dept,
    }


# --------------------------------------------------------------------------- #
#  per-company aggregates                                                     #
# --------------------------------------------------------------------------- #
REMOTE_FULL = {"remote", "full remote"}


def aggregate(jobs, now):
    cats = Counter(j.get("category") for j in jobs if j.get("category"))
    contracts = Counter(j.get("contract") for j in jobs if j.get("contract"))
    cities = Counter(j.get("city") for j in jobs if j.get("city") and j["city"] != "Remote")
    stack = Counter()
    for j in jobs:
        for s in (j.get("stack") or []):
            if page_slug(s) not in STACK_DENY:
                stack[s] += 1
    remote_n = sum(1 for j in jobs
                   if j.get("city") == "Remote" or (j.get("remote") or "") in REMOTE_FULL
                   or (j.get("remote_detail") or "") in ("full remote", "hybride"))
    xp = Counter(j.get("experience") for j in jobs if j.get("experience"))
    xp_years = [j["experience_min_years"] for j in jobs
                if isinstance(j.get("experience_min_years"), (int, float))]
    salaries = [j["salary"] for j in jobs if j.get("salary")]
    benefits = Counter()
    for j in jobs:
        benefits.update(b for b in (j.get("benefits_preview") or []) if b)

    def d(v):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    seen_dates = [d(j.get("first_seen")) for j in jobs]
    seen_dates = [x for x in seen_dates if x]
    posted_90 = sum(1 for x in seen_dates if (now - x).days <= 90)
    posted_30 = sum(1 for x in seen_dates if (now - x).days <= 30)
    newest = max((j.get("published_at") or j.get("first_seen") or "" for j in jobs),
                 default="")

    # 12-month histogram of first_seen (oldest -> newest)
    months, labels = [], []
    for k in range(11, -1, -1):
        y, m = now.year, now.month - k
        while m <= 0:
            m += 12
            y -= 1
        labels.append("%04d-%02d" % (y, m))
        months.append(sum(1 for x in seen_dates if x.year == y and x.month == m))

    return {
        "open_roles": len(jobs),
        "by_category": dict(cats.most_common()),
        "by_contract": dict(contracts.most_common()),
        "by_city": dict(cities.most_common()),
        "stack": [{"name": s, "n": n} for s, n in stack.most_common(28)],
        "remote_roles": remote_n,
        "experience": [{"label": lbl, "n": xp.get(lbl, 0)} for lbl in EXP_ORDER if xp.get(lbl)],
        "experience_min_years": (round(sum(xp_years) / len(xp_years), 1) if xp_years else None),
        "salary_samples": salaries[:6],
        "benefits": [b for b, _ in benefits.most_common(8)],
        "posted_30d": posted_30,
        "posted_90d": posted_90,
        "newest_at": newest or None,
        "hiring_months": {"labels": labels, "counts": months},
    }


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #
def main():
    if not os.path.exists(FEED):
        sys.exit("no %s — run jobboard/build.py first" % FEED)
    feed = json.load(open(FEED, encoding="utf-8"))
    jobs = feed.get("jobs", [])
    if not jobs:
        sys.exit("feed has no jobs")
    now = datetime.now(timezone.utc)
    dirs = load_directories()

    groups = {}
    for j in jobs:
        key = _slug(j.get("company"))
        if not key:
            continue
        groups.setdefault(key, []).append(j)

    out = []
    with_profile = with_eco = 0
    for key, cjobs in groups.items():
        # display name: the most common exact spelling in the feed
        name = Counter(j.get("company") for j in cjobs).most_common(1)[0][0]
        slug = page_slug(name)

        profile = None
        for j in cjobs:
            if j.get("source") == "wttj":
                profile = load_wttj_org_for(j)
                if profile:
                    break

        logo = (profile or {}).get("logo") or next(
            (j.get("logo") for j in cjobs if j.get("logo")), None)
        domain = None
        if profile and profile.get("socials", {}).get("website"):
            m = re.search(r"https?://([^/]+)", profile["socials"]["website"])
            if m:
                domain = m.group(1).lstrip("www.")
        extras = directory_extras(name, domain, dirs)

        agg = aggregate(cjobs, now)
        city = (agg["by_city"] and next(iter(agg["by_city"]))) or extras.get("dir_city") \
            or (profile or {}).get("hq_city")
        sources = sorted({j.get("source") for j in cjobs if j.get("source")})

        rec = {
            "slug": slug,
            "name": name,
            "logo": logo,
            "city": city,
            "domain": domain,
            "sources": sources,
            "profile": profile,
            "ecosystems": extras["ecosystems"],
            "tags": extras["tags"],
            "ats": extras["ats"],
            "careers_url": extras["careers_url"] or next(
                (j.get("careers_url") for j in cjobs if j.get("careers_url")), None),
            "dept": extras["dept"],
            "type": company_type(name, (profile or {}).get("headcount")),
        }
        rec.update(agg)
        out.append(rec)
        with_profile += bool(profile)
        with_eco += bool(extras["ecosystems"])

    # slug collisions (rare) -> suffix with a short hash of the key
    seen = {}
    for rec in out:
        if rec["slug"] in seen and seen[rec["slug"]] != rec["name"]:
            rec["slug"] = "%s-%s" % (rec["slug"], _slug(rec["name"])[:4] or "x")
        seen[rec["slug"]] = rec["name"]

    out.sort(key=lambda r: (-r["open_roles"], r["name"].lower()))
    payload = {
        "generated_at": feed.get("generated_at"),
        "region": feed.get("region"),
        "count": len(out),
        "companies": out,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print("build_companies: %d companies (%d with WTTJ profile, %d in an ecosystem)"
          % (len(out), with_profile, with_eco), file=sys.stderr)
    print("  wrote %s" % OUT, file=sys.stderr)


if __name__ == "__main__":
    main()
