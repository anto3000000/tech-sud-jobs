#!/usr/bin/env python3
"""Layer 1 — the "aspirateur global": Welcome to the Jungle.

WTTJ's job search is a public Algolia index. One `browse` sweep with the
region facet returns *every* active PACA posting (company, city, contract,
salary, technos, direct link). We then keep only Tech / Data / Product roles
with the shared title classifier (WTTJ's own `profession` facet only covers
~12 % of aggregated listings, so we can't rely on it).

    python jobboard/sources/wttj.py                       # PACA, tech-only
    python jobboard/sources/wttj.py --all                 # PACA, every role
    python jobboard/sources/wttj.py --state "Occitanie"   # another region
    python jobboard/sources/wttj.py --keep-adjacent       # + IT support / tech sales
    python jobboard/sources/wttj.py -o jobboard/data/wttj_paca.json

Credentials live in WTTJ's public `/api/env` (browser search key, referer
restricted). Refresh them with `--refresh-keys` if the run starts 403-ing.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from jobboard.classify import classify, CORE  # noqa: E402

# public browser credentials (referer-locked, safe to ship); auto-refreshed
DEFAULT_APP_ID = "CSEKHVMS53"
DEFAULT_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wk_cms_jobs_production"
REFERER = "https://www.welcometothejungle.com/"
ENV_URL = "https://www.welcometothejungle.com/api/env"

STATE_PACA = "Provence-Alpes-Cote d'Azur"   # exact Algolia facet value (ascii-folded)

ATTRS = [
    "name", "slug", "reference", "objectID", "organization", "offices", "office",
    "contract_type", "profession", "remote", "published_at",
    "salary_minimum", "salary_maximum", "salary_period", "salary_currency",
    "salary_yearly_minimum", "experience_level_minimum", "language", "sectors",
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _post(url, payload, app_id, api_key, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Referer": REFERER,
        "Origin": "https://www.welcometothejungle.com",
        "X-Algolia-API-Key": api_key,
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-Agent": "Algolia for JavaScript (4.26.0); Browser",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def refresh_keys():
    """Scrape the current public search key from WTTJ's /api/env."""
    req = urllib.request.Request(ENV_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode("utf-8", "replace")
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    env = json.loads(txt)
    return (env.get("PUBLIC_ALGOLIA_APPLICATION_ID", DEFAULT_APP_ID),
            env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT", DEFAULT_API_KEY))


def browse_state(state, app_id, api_key, page_size=1000, sleep=0.3, verbose=True):
    """Pull every hit for one region via the cursor-based browse endpoint
    (bypasses Algolia's 1000-result window)."""
    base = "https://%s-dsn.algolia.net/1/indexes/%s/browse" % (app_id, INDEX)
    payload = {
        "query": "",
        "hitsPerPage": page_size,
        "facetFilters": [["offices.state:%s" % state]],
        "attributesToRetrieve": ATTRS,
        "attributesToHighlight": [],
    }
    hits, cursor, page = [], None, 0
    while True:
        p = dict(payload)
        if cursor:
            p["cursor"] = cursor
        data = _post(base, p, app_id, api_key)
        batch = data.get("hits", [])
        hits.extend(batch)
        page += 1
        if verbose:
            print("  page %d  +%d  (total %d)" % (page, len(batch), len(hits)), file=sys.stderr)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(sleep)
    return hits


CONTRACT_FR = {
    "FULL_TIME": "CDI", "TEMPORARY": "CDD", "INTERNSHIP": "Stage",
    "APPRENTICESHIP": "Alternance", "FREELANCE": "Freelance", "PART_TIME": "Temps partiel",
    "VIE": "VIE", "GRADUATE_PROGRAM": "Graduate", "OTHER": "Autre", "VOLUNTEER": "Bénévolat",
}
REMOTE_FR = {
    "fulltime": "full remote", "full": "full remote", "total": "full remote",
    "partial": "hybride", "punctual": "ponctuel", "none": "sur site",
    "no": "sur site", "unknown": "", "": "",
}


def normalize(hit, category):
    org = hit.get("organization") or {}
    offices = hit.get("offices") or ([hit["office"]] if hit.get("office") else [])
    # prefer the office that actually sits in the region we filtered on
    o0 = next((o for o in offices if "provence" in (o.get("state") or "").lower()
               or "azur" in (o.get("state") or "").lower()), offices[0] if offices else {})
    org_slug = org.get("slug")
    job_slug = hit.get("slug")
    url = None
    if org_slug and job_slug:
        url = "https://www.welcometothejungle.com/fr/companies/%s/jobs/%s" % (org_slug, job_slug)
        if hit.get("reference"):
            url += "?o=%s" % hit["reference"]
    prof = hit.get("profession") or {}
    pname = (prof.get("name") or {}).get("fr") if isinstance(prof.get("name"), dict) else None
    sal_min = hit.get("salary_minimum") or hit.get("salary_yearly_minimum")
    sal_max = hit.get("salary_maximum")
    salary = None
    if sal_min or sal_max:
        cur = hit.get("salary_currency") or "EUR"
        per = {"year": "/an", "month": "/mois", "day": "/j", "hour": "/h"}.get(
            hit.get("salary_period"), "")
        if sal_min and sal_max and sal_min != sal_max:
            salary = "%s–%s %s%s" % (int(sal_min), int(sal_max), cur, per)
        else:
            salary = "%s %s%s" % (int(sal_min or sal_max), cur, per)
    cities = sorted({o.get("city") for o in offices if o.get("city")})
    return {
        "id": "wttj:%s" % hit.get("objectID"),
        "title": (hit.get("name") or "").strip(),
        "company": org.get("name") or org_slug or "?",
        "company_slug": org_slug,
        "city": o0.get("city"),
        "cities": cities,
        "department": o0.get("district"),
        "region": o0.get("state"),
        "contract": CONTRACT_FR.get(hit.get("contract_type"), hit.get("contract_type")),
        "remote": REMOTE_FR.get((hit.get("remote") or "").lower(), hit.get("remote")),
        "category": category,
        "profession": pname,
        "salary": salary,
        "experience_min_years": hit.get("experience_level_minimum"),
        "published_at": hit.get("published_at"),
        "url": url,
        "source": "wttj",
    }


def run(state, tech_only=True, keep_adjacent=True, app_id=None, api_key=None):
    app_id = app_id or DEFAULT_APP_ID
    api_key = api_key or DEFAULT_API_KEY
    try:
        raw = browse_state(state, app_id, api_key)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            print("  %s -> refreshing keys from /api/env ..." % e.code, file=sys.stderr)
            app_id, api_key = refresh_keys()
            raw = browse_state(state, app_id, api_key)
        else:
            raise

    seen, out = set(), []
    kept_cat = dict.fromkeys(["eng", "data", "product", "design", "tech-adjacent"], 0)
    for h in raw:
        oid = h.get("objectID")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        cat = classify(h.get("name"), (h.get("profession") or {}).get("name", {}).get("fr")
                       if isinstance((h.get("profession") or {}).get("name"), dict) else None)
        if tech_only:
            if cat is None:
                continue
            if cat == "tech-adjacent" and not keep_adjacent:
                continue
        if cat:
            kept_cat[cat] = kept_cat.get(cat, 0) + 1
        out.append(normalize(h, cat))

    out.sort(key=lambda j: j.get("published_at") or "", reverse=True)
    return raw, out, kept_cat


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "jobboard", "data", "wttj_paca.json"))
    ap.add_argument("--state", default=STATE_PACA, help="Algolia offices.state facet value")
    ap.add_argument("--all", action="store_true", help="keep every role, not just tech")
    ap.add_argument("--keep-adjacent", action="store_true", default=True)
    ap.add_argument("--no-adjacent", dest="keep_adjacent", action="store_false",
                    help="drop IT support / tech sales / consulting")
    ap.add_argument("--refresh-keys", action="store_true")
    ap.add_argument("--raw-out", help="also dump the untouched Algolia hits here")
    args = ap.parse_args()

    app_id, api_key = (refresh_keys() if args.refresh_keys else (DEFAULT_APP_ID, DEFAULT_API_KEY))
    print("Browsing WTTJ index=%s state=%r ..." % (INDEX, args.state), file=sys.stderr)

    raw, jobs, kept_cat = run(args.state, tech_only=not args.all,
                              keep_adjacent=args.keep_adjacent, app_id=app_id, api_key=api_key)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    if args.raw_out:
        with open(args.raw_out, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    n_comp = len({j["company"] for j in jobs})
    print("\n  region postings scanned : %d" % len(raw), file=sys.stderr)
    print("  kept (%s)          : %d  from %d companies"
          % ("all" if args.all else "tech", len(jobs), n_comp), file=sys.stderr)
    if not args.all:
        print("  by category            : %s"
              % ", ".join("%s=%d" % (k, v) for k, v in kept_cat.items() if v), file=sys.stderr)
    top = {}
    for j in jobs:
        top[j["company"]] = top.get(j["company"], 0) + 1
    print("  top employers          : %s" % ", ".join(
        "%s(%d)" % (c, n) for c, n in sorted(top.items(), key=lambda x: -x[1])[:12]), file=sys.stderr)
    print("\nwrote %s" % args.output, file=sys.stderr)


if __name__ == "__main__":
    main()
