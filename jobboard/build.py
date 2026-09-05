#!/usr/bin/env python3
"""Merge every source into the single feed the site reads.

    python jobboard/build.py

Inputs (whichever exist):
    jobboard/data/wttj_paca.json     Layer 1  (already normalized, tech-filtered)
    jobboard/data/ats_jobs.json      Layer 3  (fetch_jobs.py output, raw shape)

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


def _slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _norm_title(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(?\b[hf]/?[hfx]\b\)?|\bm/f\b|\bw/m\b|\(f/h\)|\(h/f\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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
        if not r.get("category"):
            r["category"] = classify(r.get("title"), r.get("profession"))
    return rows


def load_ats():
    p = os.path.join(DATA, "ats_jobs.json")
    if not os.path.exists(p):
        return []
    raw = json.load(open(p, encoding="utf-8"))
    out = []
    for j in raw:
        loc = j.get("location") or ""
        remote = j.get("remote")
        is_remote = bool(remote) or bool(REMOTE_RX.search(loc))
        if not (_is_paca(loc, j.get("department")) or is_remote):
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


SOURCE_RANK = {"wttj": 0}   # everything else (direct ATS) ranks higher = preferred


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
        # prefer a non-wttj (direct) source, then the one with a salary, then newest
        def score(x):
            return (
                0 if x.get("source") == "wttj" else 1,
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
    ats = load_ats()
    print("  wttj rows : %d" % len(wttj), file=sys.stderr)
    print("  ats  rows : %d (PACA+tech)" % len(ats), file=sys.stderr)

    lead_contract = re.compile(
        r"^\s*(cdi|cdd|stage|stagiaire|alternance|apprentissage|freelance|vie|"
        r"internship|apprenticeship|contract|full[ -]?time|part[ -]?time|interim)\b"
        r"[\s:_/–-]*", re.I)
    for r in wttj + ats:
        t = (r.get("title") or "").strip()
        stripped = lead_contract.sub("", t).strip(" :–-—/")
        if len(stripped) > 6:
            r["title"] = stripped
        if r.get("city") not in (None, "Remote"):
            r["city"] = canon_city(r["city"])
        r["cities"] = sorted({canon_city(c) for c in (r.get("cities") or []) if c})

    merged = dedupe(wttj + ats)
    # keep core + adjacent; expose the split to the UI via `category`
    merged = [m for m in merged if m.get("category")]

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = stamp_first_seen(merged, now_iso)
    merged.sort(key=lambda j: (j.get("first_seen") or "", j.get("published_at") or ""),
                reverse=True)

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
