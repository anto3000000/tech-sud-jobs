#!/usr/bin/env python3
"""Enrich WTTJ jobs with their detail payload.

The Algolia index only carries title / company / city / contract. The public
detail API
    https://api.welcometothejungle.com/api/v1/organizations/<org>/jobs/<slug>
adds the tech stack (`tools`), the full description, the experience level, the
remote policy, a real apply URL (straight to the employer's ATS) and the
company logo — everything that makes the board searchable and worth reading.

    python jobboard/sources/wttj_enrich.py                 # enrich data/wttj_paca.json in place
    python jobboard/sources/wttj_enrich.py --workers 12
    python jobboard/sources/wttj_enrich.py --max-age 3     # refetch entries older than 3 days
    python jobboard/sources/wttj_enrich.py --force         # ignore cache

Responses are cached under data/cache/wttj/<objectID>.json, so re-runs only
hit the network for new or stale jobs.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from jobboard.classify import classify  # noqa: E402
DATA = os.path.join(ROOT, "jobboard", "data")
CACHE = os.path.join(DATA, "cache", "wttj")
API = "https://api.welcometothejungle.com/api/v1/organizations/%s/jobs/%s"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

EXP_FR = {
    "LESS_THAN_6_MONTHS": "Débutant", "6_MONTHS_TO_1_YEAR": "< 1 an",
    "1_TO_2_YEARS": "1–2 ans", "2_TO_5_YEARS": "2–5 ans", "5_TO_7_YEARS": "5–7 ans",
    "7_TO_10_YEARS": "7–10 ans", "MORE_THAN_10_YEARS": "> 10 ans",
}
REMOTE_FR = {
    "full": "full remote", "fulltime": "full remote", "total": "full remote",
    "partial": "hybride", "punctual": "ponctuel", "none": "sur site",
    "no": "sur site", "unknown": "", "": "",
}


def _strip_html(s):
    s = re.sub(r"<(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    s = s.strip()
    # peel off up to 3 leading noise lines: bare section headers, location lines,
    # tag-line slogans — anything short and heading-shaped before the real copy
    HEAD = re.compile(
        r"^(company description|job description|descriptif du poste|description du poste|"
        r"description|le poste|vos missions|missions|votre mission|contexte|"
        r"à propos( de nous| du poste)?|présentation( de l'entreprise)?|"
        r"qui sommes[- ]nous ?\??|l'entreprise|the role|about the role|about us|"
        r"construisons ensemble un avenir de confiance)\s*:?\s*$", re.I)
    LOC = re.compile(r"^(lieu|location|ville|poste basé à)\s*:.*$", re.I)
    for _ in range(3):
        line, _, rest = s.partition("\n")
        if HEAD.match(line.strip()) or LOC.match(line.strip()):
            s = rest.strip()
        else:
            break
    return s


def _parse_url(job_url):
    """https://www.welcometothejungle.com/fr/companies/<org>/jobs/<slug>?o=… -> (org, slug)"""
    if not job_url:
        return None, None
    m = re.search(r"/companies/([^/]+)/jobs/([^/?#]+)", job_url)
    return (m.group(1), m.group(2)) if m else (None, None)


def _fetch(org, slug, timeout=20):
    url = API % (org, slug)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Referer": "https://www.welcometothejungle.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _cache_path(oid):
    return os.path.join(CACHE, "%s.json" % oid)


def _cache_fresh(oid, max_age_days):
    p = _cache_path(oid)
    if not os.path.exists(p):
        return None
    if max_age_days is not None:
        age = time.time() - os.path.getmtime(p)
        if age > max_age_days * 86400:
            return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except (ValueError, OSError):
        return None


def distill(detail):
    """Full API payload -> the handful of fields the board needs."""
    j = detail.get("job", detail) or {}
    org = j.get("organization") or {}
    tools = [t.get("name") for t in (j.get("tools") or []) if t.get("name")]
    # WTTJ 'skills' are soft skills / values, keep them separate & de-noised
    skills = []
    for s in (j.get("skills") or []):
        nm = (s.get("name") or {})
        nm = nm.get("fr") or nm.get("en") if isinstance(nm, dict) else nm
        if nm:
            skills.append(nm)
    benefits = j.get("benefits") or {}
    bfr = benefits.get("FR") or benefits.get("EN") or {}
    bprev = []
    for b in (bfr.get("preview") or [])[:6]:
        nm = (b.get("name") or {})
        nm = nm.get("fr") or nm.get("en") if isinstance(nm, dict) else nm
        if nm:
            bprev.append(nm)
    logo = ((org.get("logo") or {}).get("thumb") or {}).get("url") \
        or (org.get("logo") or {}).get("url")
    desc = _strip_html(j.get("description"))
    profile = _strip_html(j.get("profile"))
    remote = (j.get("remote") or "").lower()
    return {
        "stack": tools,
        "soft_skills": skills,
        "description": desc[:4000] or None,
        "description_excerpt": (desc[:340].rsplit(" ", 1)[0] + "…") if len(desc) > 340 else (desc or None),
        "profile_excerpt": (profile[:280].rsplit(" ", 1)[0] + "…") if len(profile) > 280 else (profile or None),
        "experience": EXP_FR.get(j.get("experience_level")),
        "education_level": j.get("education_level"),
        "remote_detail": REMOTE_FR.get(remote, remote) or None,
        "apply_url": j.get("apply_url") or None,
        "ats": j.get("ats") or None,
        "logo": logo,
        "benefits_preview": bprev,
        "benefits_count": (bfr.get("count") if isinstance(bfr.get("count"), int) else None),
        "start_date": j.get("start_date"),
        "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def enrich_one(job, max_age_days, force):
    oid = (job.get("id") or "").split(":", 1)[-1]
    org, slug = _parse_url(job.get("url"))
    if not oid or not org or not slug:
        return job, "skip:no-url"
    cached = None if force else _cache_fresh(oid, max_age_days)
    if cached is not None:
        job.update(distill(cached))
        _reclassify(job)
        return job, "cache"
    try:
        detail = _fetch(org, slug)
    except urllib.error.HTTPError as e:
        return job, "http:%s" % e.code
    except Exception as e:  # noqa: BLE001
        return job, "err:%s" % type(e).__name__
    os.makedirs(CACHE, exist_ok=True)
    with open(_cache_path(oid), "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False)
    job.update(distill(detail))
    _reclassify(job)
    return job, "fetch"


def _reclassify(job):
    """Now that we have the tools list, let the classifier revisit the call."""
    new = classify(job.get("title"), job.get("profession"), stack=job.get("stack"))
    if new:
        job["category"] = new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", default=os.path.join(DATA, "wttj_paca.json"))
    ap.add_argument("-o", "--output", default=None, help="default: overwrite --input")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-age", type=float, default=7, help="refetch cache older than N days (0 = never expire)")
    ap.add_argument("--force", action="store_true", help="ignore cache entirely")
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args()

    jobs = json.load(open(args.input, encoding="utf-8"))
    max_age = None if args.max_age == 0 else args.max_age
    print("Enriching %d WTTJ jobs (workers=%d) ..." % (len(jobs), args.workers), file=sys.stderr)

    stats, t0 = {}, time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(enrich_one, j, max_age, args.force): i for i, j in enumerate(jobs)}
        for fut in concurrent.futures.as_completed(futs):
            j, tag = fut.result()
            jobs[futs[fut]] = j
            stats[tag.split(":")[0]] = stats.get(tag.split(":")[0], 0) + 1
            done += 1
            if done % 50 == 0:
                print("  %d/%d  %s" % (done, len(jobs), stats), file=sys.stderr)
            if tag == "fetch":
                time.sleep(args.sleep)

    out = args.output or args.input
    with open(out, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)

    with_stack = sum(1 for j in jobs if j.get("stack"))
    with_apply = sum(1 for j in jobs if j.get("apply_url"))
    with_desc = sum(1 for j in jobs if j.get("description"))
    print("\n  %s  in %.1fs" % (stats, time.time() - t0), file=sys.stderr)
    print("  stack: %d/%d   apply_url: %d   description: %d"
          % (with_stack, len(jobs), with_apply, with_desc), file=sys.stderr)
    # most common tools, quick sanity
    from collections import Counter
    c = Counter(t for j in jobs for t in (j.get("stack") or []))
    print("  top stack: %s" % ", ".join("%s(%d)" % kv for kv in c.most_common(20)), file=sys.stderr)
    print("\nwrote %s" % out, file=sys.stderr)


if __name__ == "__main__":
    main()
