#!/usr/bin/env python3
"""Merge every source into the single feed the site reads.

    python jobboard/build.py

Inputs (whichever exist):
    jobboard/data/wttj_paca.json          Layer 1  (already normalized, tech-filtered)
    jobboard/data/francetravail_paca.json Layer 1c (already normalized, tech-filtered)
    jobboard/data/ats_jobs.json           Layer 3  (fetch_jobs.py output, raw shape)

Steps: normalize -> keep PACA + Tech/Data/Product -> de-duplicate
(company+title; a direct-ATS link beats a WTTJ link) -> sort newest first.

Outputs:
    jobboard/site/jobs.json          feed for the static site
    jobboard/data/jobs.json          same, for inspection
"""
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from jobboard.classify import classify, CORE  # noqa: E402

DATA = os.path.join(HERE, "data")
SITE = os.path.join(HERE, "site")

PACA_RX = re.compile(
    r"\b(aix|marseille|nice|sophia|antipolis|antibes|cannes|toulon|avignon|"
    r"rousset|carros|marignane|valbonne|gemenos|la ciotat|manosque|gap|"
    r"provence|paca|alpes-maritimes|bouches-du-rh|\bvar\b|vaucluse|hautes-alpes|"
    r"alpes-de-haute|cote d.azur|\b06\d{3}\b|\b13\d{3}\b|\b83\d{3}\b|\b84\d{3}\b|"
    r"\b(06|13|83|84|04|05)\b)",
    re.I,
)
REMOTE_RX = re.compile(r"\b(remote|t[eé]l[eé]travail|full.?remote|100%\s*remote|"
                       r"anywhere|partout en france)\b", re.I)
# ATS boards are worldwide; a Lever/Greenhouse feed for an FR company still lists
# its Madrid / NYC / Bangalore roles. Reject anything explicitly anchored abroad.
FOREIGN_RX = re.compile(
    r"\b(madrid|barcelona|sevilla|lisbon|lisboa|porto|london|manchester|dublin|"
    r"berlin|munich|münchen|hamburg|frankfurt|cologne|amsterdam|rotterdam|"
    r"brussels|bruxelles|antwerp|milan|milano|rome|roma|turin|madrid|"
    r"new york|nyc|san francisco|sfo|seattle|austin|boston|chicago|denver|"
    r"atlanta|miami|los angeles|toronto|montreal|vancouver|"
    r"sydney|melbourne|singapore|bangalore|bengaluru|mumbai|hyderabad|tokyo|"
    r"warsaw|warszawa|krakow|kraków|prague|praha|bucharest|bucurești|sofia|"
    r"tallinn|vilnius|casablanca|tunis|cairo|dubai|tel aviv|bangkok|"
    r"são paulo|sao paulo|mexico city|bogota|bogotá|buenos aires|"
    r"remote\s*[-–,]?\s*(?:us|usa|u\.s\.|uk|emea|apac|latam|na\b|north america|"
    r"germany|spain|italy|india|poland|portugal|brazil|canada|australia))\b",
    re.I,
)


def _is_foreign(*fields):
    return bool(FOREIGN_RX.search(" ".join(str(f or "") for f in fields)))


def _slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _norm_title(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(?\b[hf]/?[hfx]\b\)?|\bm/f\b|\bw/m\b|\(f/h\)|\(h/f\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _page_slugify(s):
    """dash-slug for URLs — MUST stay in sync with render_pages.slugify."""
    s = str(s or "").lower()
    for a, b in (("c++", "cpp"), ("c#", "csharp"), (".net", "dotnet"), ("f#", "fsharp"),
                 ("node.js", "nodejs"), ("next.js", "nextjs")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)


def job_slug(j):
    """Stable per-offer slug -> site/offre/<slug>.html.
    MUST stay in sync with render_pages.job_slug (the SPA links to these files)."""
    base = _page_slugify("%s-%s" % (j.get("company", ""), j.get("title", "")))[:70].strip("-")
    h = hashlib.sha1(j["id"].encode()).hexdigest()[:6]
    return "%s-%s" % (base, h) if base else h


def canon_city(c):
    if not c:
        return None
    c = re.sub(r"\s+", " ", str(c)).strip(" ,-")
    low = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode().lower()
    low = re.sub(r"[\s'-]+", " ", low).strip()
    table = {
        "aix en provence": "Aix-en-Provence", "aix": "Aix-en-Provence",
        "sophia antipolis": "Sophia Antipolis", "biot sophia antipolis": "Sophia Antipolis",
        "la seyne sur mer": "La Seyne-sur-Mer", "cagnes sur mer": "Cagnes-sur-Mer",
        "saint laurent du var": "Saint-Laurent-du-Var",
        "mandelieu la napoule": "Mandelieu-la-Napoule",
        "salon de provence": "Salon-de-Provence", "l isle sur la sorgue": "L'Isle-sur-la-Sorgue",
        "les pennes mirabeau": "Les Pennes-Mirabeau", "aubagne": "Aubagne",
    }
    if low in table:
        return table[low]
    return c[:1].upper() + c[1:]


# raw contract strings are a mess across the three sources: "FullTime",
# "Permanent Contract", "fulltime_permanent", "CDD - 7 Mois", English vs French,
# and — worst — stages / alternances mis-tagged "CDI" / "Full-time" by the ATS.
# Fold everything to a small closed set. The title wins when it explicitly names
# a stage / alternance / VIE, because that is exactly where the source lies.
_CONTRACT_TITLE_RX = [
    ("Stage",      re.compile(r"\b(?:stage|stagiaire|internship|intern)\b", re.I)),
    ("Alternance", re.compile(r"\b(?:alternance|alternant\w*|apprenti\w*|"
                              r"contrat\s+pro\w*|work[ -]?study)\b", re.I)),
    ("VIE",        re.compile(r"\bVIE\b|\bV\.I\.E\.?\b|[Vv]olontariat [Ii]nternational")),
]
_CONTRACT_MAP = {
    "cdi": "CDI", "fulltime": "CDI", "full time": "CDI", "full-time": "CDI",
    "permanent": "CDI", "permanent contract": "CDI", "fulltime_permanent": "CDI",
    "permanent full time employee": "CDI", "regular": "CDI",
    "cdd": "CDD", "temporary": "CDD", "temporary contract": "CDD",
    "contract": "CDD", "fixed-term": "CDD", "fulltime_fixed_term": "CDD",
    "stage": "Stage", "stagiaire": "Stage", "intern": "Stage", "internship": "Stage",
    "alternance": "Alternance", "apprentissage": "Alternance",
    "apprenticeship": "Alternance", "contrat de professionnalisation": "Alternance",
    "vie": "VIE", "v.i.e": "VIE", "volontariat international": "VIE",
    "freelance": "Freelance", "contractor": "Freelance", "independant": "Freelance",
    "interim": "Intérim", "temp": "Intérim",
    "parttime": "Temps partiel", "part time": "Temps partiel",
    "part-time": "Temps partiel",
}
_CONTRACT_FRAGS = [
    ("stagiaire", "Stage"), ("stage", "Stage"), ("internship", "Stage"),
    ("intern", "Stage"), ("alternance", "Alternance"), ("apprenti", "Alternance"),
    ("fixed", "CDD"), ("cdd", "CDD"), ("temporary", "CDD"),
    ("cdi", "CDI"), ("permanent", "CDI"), ("full", "CDI"),
    ("freelance", "Freelance"), ("interim", "Intérim"), ("vie", "VIE"),
]


def canon_contract(raw, title=None):
    for label, rx in _CONTRACT_TITLE_RX:
        if title and rx.search(title):
            return label
    if not raw:
        return None
    key = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode().lower()
    key = re.sub(r"\s+", " ", key).strip(" .")
    key = re.sub(r"\s*[-–—/]\s*\d+\s*(?:mois|month|months|an|ans|year|years|"
                 r"semaines?|weeks?|jours?|days?)\b.*$", "", key).strip()
    if key in _CONTRACT_MAP:
        return _CONTRACT_MAP[key]
    for frag, label in _CONTRACT_FRAGS:
        if frag in key:
            return label
    return raw  # unknown value: keep it rather than hide the job from the facet


def _is_paca(*fields):
    hay = " ".join(str(f or "") for f in fields)
    return bool(PACA_RX.search(hay))


def load_wttj():
    p = os.path.join(DATA, "wttj_paca.json")
    if not os.path.exists(p):
        return []
    rows = json.load(open(p, encoding="utf-8"))
    for r in rows:
        r.setdefault("source", "wttj")
        # always trust the current classifier (it has the same title + profession
        # + persisted stack that wttj_enrich used): keeps pre-tagged rows in sync
        # with rule changes without a re-scrape — e.g. "Business Developer" -> None
        # (dropped), "Business Analyst" -> data. None rows are filtered in main().
        r["category"] = classify(r.get("title"), r.get("profession"), stack=r.get("stack"))
    return rows


def load_ft():
    """Layer 1c — France Travail. Rows are already normalized + tech-filtered
    by sources/francetravail.py; re-run the classifier to stay in sync with
    rule changes, and keep only PACA-located ones."""
    p = os.path.join(DATA, "francetravail_paca.json")
    if not os.path.exists(p):
        return []
    rows = json.load(open(p, encoding="utf-8"))
    out = []
    for r in rows:
        r.setdefault("source", "francetravail")
        r["category"] = classify(r.get("title"), r.get("profession"))
        if not r.get("category"):
            continue
        if not (_is_paca(r.get("city"), r.get("department"), r.get("region"))
                or REMOTE_RX.search(str(r.get("remote") or ""))):
            continue
        out.append(r)
    return out


def load_ats():
    p = os.path.join(DATA, "ats_jobs.json")
    if not os.path.exists(p):
        return []
    raw = json.load(open(p, encoding="utf-8"))
    out = []
    for j in raw:
        loc = j.get("location") or ""
        remote = str(j.get("remote") or "")
        # a foreign office (or "Remote - US") is a hard no, whatever else matches
        if _is_foreign(loc):
            continue
        # "hybrid" / "none" are not remote; only an explicit remote marker counts.
        # NB: department is a free-text category (Aircall ships "13009 - Onboarding")
        # -> never feed it to the PACA postal-code regex.
        is_remote = bool(REMOTE_RX.search(loc)) or remote.lower() in ("remote", "fully", "true", "yes")
        if not (_is_paca(loc) or is_remote):
            continue
        cat = classify(j.get("title"), j.get("department"))
        if cat is None:
            continue
        city = None
        m = re.split(r"[,/(]", loc)
        if m and m[0].strip():
            city = m[0].strip()
        out.append({
            "id": "ats:%s:%s:%s" % (
                j.get("source_ats"), _slug(j.get("company")),
                hashlib.sha1(((j.get("url") or "") + (j.get("title") or "")).encode()).hexdigest()[:10]),
            "title": (j.get("title") or "").strip(),
            "company": j.get("company"),
            "company_slug": _slug(j.get("company")),
            "city": city if (city and _is_paca(city)) else ("Remote" if is_remote else city),
            "cities": [city] if city else [],
            "department": j.get("department"),
            "region": "Provence-Alpes-Côte d'Azur" if _is_paca(loc) else None,
            "contract": j.get("contract"),
            "remote": "remote" if is_remote else None,
            "category": cat,
            "profession": None,
            "salary": None,
            "experience_min_years": None,
            "published_at": j.get("published_at"),
            "url": j.get("url"),
            "source": j.get("source_ats") or "ats",
            "careers_url": j.get("careers_url"),
        })
    return out


# link-quality rank when the same (company, title) shows up in several sources:
# a direct ATS link beats a WTTJ link beats a France Travail aggregator link.
SOURCE_RANK = {"francetravail": 0, "wttj": 1}   # anything else (direct ATS) = 2


def dedupe(rows):
    best = {}
    for r in rows:
        key = (_slug(r.get("company")), _norm_title(r.get("title")))
        if not key[0] or not key[1]:
            key = (r.get("id"),)
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        # prefer the best link source, then the one with a salary, then newest
        def score(x):
            return (
                SOURCE_RANK.get(x.get("source"), 2),
                1 if x.get("salary") else 0,
                x.get("published_at") or "",
            )
        best[key] = max((cur, r), key=score)
    return list(best.values())


SEEN_PATH = os.path.join(DATA, "seen.json")
NEW_WINDOW_DAYS = 10


def stamp_first_seen(jobs, now_iso):
    """Persist id -> first date we ever saw it; stamp first_seen / is_new on each
    job so the daily cron can surface genuinely new postings (WTTJ published_at
    is unreliable — often a re-publication date)."""
    try:
        seen = json.load(open(SEEN_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEW_WINDOW_DAYS)).isoformat()
    for j in jobs:
        jid = j.get("id")
        if not jid:
            continue
        first = seen.get(jid)
        if not first:
            first = seen[jid] = now_iso
        j["first_seen"] = first
        j["is_new"] = first >= cutoff
    # prune ids that fell out of the feed long ago -> keep the file bounded
    live = {j.get("id") for j in jobs}
    for jid in list(seen):
        if jid not in live and seen[jid] < cutoff:
            del seen[jid]
    os.makedirs(DATA, exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)
    return seen


def main():
    wttj = load_wttj()
    ft = load_ft()
    ats = load_ats()
    print("  wttj rows : %d" % len(wttj), file=sys.stderr)
    print("  ft   rows : %d (PACA+tech)" % len(ft), file=sys.stderr)
    print("  ats  rows : %d (PACA+tech)" % len(ats), file=sys.stderr)

    lead_contract = re.compile(
        r"^\s*(cdi|cdd|stage|stagiaire|alternance|apprentissage|freelance|vie|"
        r"internship|apprenticeship|contract|full[ -]?time|part[ -]?time|interim)\b"
        r"[\s:_/–-]*", re.I)
    for r in wttj + ft + ats:
        t = (r.get("title") or "").strip()
        # fold the contract to the closed set, letting the *raw* title (before we
        # strip its "Stage -" / "Alternance :" prefix below) override a source
        # that mis-tagged a work-study role as CDI / Full-time
        r["contract"] = canon_contract(r.get("contract"), t)
        stripped = lead_contract.sub("", t).strip(" :–-—/")
        if len(stripped) > 6:
            r["title"] = stripped
        if r.get("city") not in (None, "Remote"):
            r["city"] = canon_city(r["city"])
        r["cities"] = sorted({canon_city(c) for c in (r.get("cities") or []) if c})

    merged = dedupe(wttj + ft + ats)
    # keep core + adjacent; expose the split to the UI via `category`
    merged = [m for m in merged if m.get("category")]

    # CI guard: if a source API silently changes shape we can end up with an
    # almost-empty feed. Fail loudly instead of deploying it. Override locally
    # with ALLOW_SMALL_FEED=1 (e.g. when testing on a partial dataset).
    min_jobs = int(os.environ.get("MIN_JOBS", "150"))
    if len(merged) < min_jobs and not os.environ.get("ALLOW_SMALL_FEED"):
        sys.exit("ERROR: only %d jobs after merge (< %d) — refusing to write a "
                 "near-empty feed. Set ALLOW_SMALL_FEED=1 to override."
                 % (len(merged), min_jobs))

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = stamp_first_seen(merged, now_iso)
    merged.sort(key=lambda j: (j.get("first_seen") or "", j.get("published_at") or ""),
                reverse=True)

    for m in merged:
        m["slug"] = job_slug(m)   # link target for the SPA -> site/offre/<slug>.html

    by_cat, by_src, by_city = {}, {}, {}
    for m in merged:
        by_cat[m["category"]] = by_cat.get(m["category"], 0) + 1
        by_src[m["source"]] = by_src.get(m["source"], 0) + 1
        c = m.get("city") or "?"
        by_city[c] = by_city.get(c, 0) + 1

    new_count = sum(1 for m in merged if m.get("is_new"))
    feed = {
        "generated_at": now_iso,
        "region": "Provence-Alpes-Côte d'Azur",
        "count": len(merged),
        "companies": len({m["company"] for m in merged}),
        "new_count": new_count,
        "by_category": by_cat,
        "by_source": by_src,
        "jobs": merged,
    }
    print("  new since last run : %d  (seen db: %d ids)" % (new_count, len(seen)), file=sys.stderr)

    os.makedirs(SITE, exist_ok=True)
    for path in (os.path.join(SITE, "jobs.json"), os.path.join(DATA, "jobs.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(feed, f, ensure_ascii=False, indent=2)

    print("\n  merged (deduped)   : %d jobs / %d companies"
          % (feed["count"], feed["companies"]), file=sys.stderr)
    print("  by category        : %s" % by_cat, file=sys.stderr)
    print("  by source          : %s" % by_src, file=sys.stderr)
    print("  top cities         : %s" % ", ".join(
        "%s(%d)" % (k, v) for k, v in sorted(by_city.items(), key=lambda x: -x[1])[:10]),
        file=sys.stderr)
    print("\nwrote jobboard/site/jobs.json + jobboard/data/jobs.json", file=sys.stderr)


if __name__ == "__main__":
    main()
