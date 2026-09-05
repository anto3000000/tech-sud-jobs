#!/usr/bin/env python3
"""Pull open jobs for every company that resolved to an API-backed ATS.

    python jobboard/fetch_jobs.py                     # uses companies.resolved.json
    python jobboard/fetch_jobs.py --paca-only         # keep only PACA / remote-FR jobs
    python jobboard/fetch_jobs.py -i companies.resolved.json -o jobs.json

Writes jobs.json (flat list) + jobs.csv. Companies whose ATS has no public
API (welcometothejungle, icims, teamtailor, custom, unresolved) are listed
in the run summary under "needs browser".
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobboard.ats.adapters import fetch_jobs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# PACA cities/departments, plus genuinely-remote markers. Bare "France" is
# deliberately excluded (would match "Tremblay-en-France" etc.).
PACA_RX = re.compile(
    r"(aix-en-provence|aix en provence|marseille|\bnice\b|sophia.?antipolis|"
    r"antibes|cannes|toulon|avignon|rousset|carros|marignane|valbonne|"
    r"gemenos|la ciotat|manosque|gardanne|vitrolles|"
    r"provence-alpes|\bpaca\b|alpes-maritimes|bouches-du-rh|\bvar\b|vaucluse|"
    r"\b06\d{3}\b|\b13\d{3}\b|"
    r"fully remote|full remote|100% remote|remote.?(?:france|europe|emea)|"
    r"télétravail total|remote.?first)",
    re.I,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", default=os.path.join(HERE, "companies.resolved.json"))
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "jobs.json"))
    ap.add_argument("--paca-only", action="store_true", help="filter to PACA / remote-FR locations")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        resolved = json.load(f)

    all_jobs = []
    needs_browser = []
    errors = []
    per_company = []

    for r in resolved:
        ats = r.get("resolved_ats")
        slug = r.get("resolved_slug")
        method = r.get("method")
        if method != "api" or not ats or not slug:
            needs_browser.append((r["name"], ats or "?", r.get("resolved_via")))
            continue
        res = fetch_jobs(ats, slug, careers_origin=r.get("careers_origin"))
        per_company.append((r["name"], ats, res.ok, len(res.jobs), res.note))
        if not res.ok and not res.jobs:
            errors.append((r["name"], ats, res.note))
            continue
        for j in res.jobs:
            d = j.as_dict()
            d.update(company=r["name"], source_ats=ats, careers_url=r.get("careers_url"),
                     fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            all_jobs.append(d)

    if args.paca_only:
        before = len(all_jobs)
        all_jobs = [j for j in all_jobs if _is_paca(j)]
        print("PACA filter: %d -> %d jobs" % (before, len(all_jobs)), file=sys.stderr)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, ensure_ascii=False, indent=2)
    _write_csv(all_jobs, os.path.splitext(args.output)[0] + ".csv")

    _summary(per_company, all_jobs, needs_browser, errors)
    print("\nwrote %s (%d jobs) + %s.csv"
          % (args.output, len(all_jobs), os.path.splitext(args.output)[0]), file=sys.stderr)


def _is_paca(job):
    hay = " ".join(str(job.get(k) or "") for k in ("location", "remote", "department"))
    return bool(PACA_RX.search(hay))


def _write_csv(jobs, path):
    cols = ["company", "title", "location", "department", "contract", "remote",
            "source_ats", "url", "published_at", "careers_url", "fetched_at"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow(j)


def _summary(per_company, jobs, needs_browser, errors):
    print("\n=== per company (API) ===")
    for name, ats, ok, n, note in sorted(per_company, key=lambda x: -x[3]):
        flag = "ok " if ok else "ERR"
        print("  %-30s %-15s %s %3d  %s" % (name[:30], ats, flag, n, note or ""))
    print("\n=== needs headless browser / dedicated scraper ===")
    for name, ats, via in needs_browser:
        print("  %-30s %-18s (%s)" % (name[:30], ats, via))
    if errors:
        print("\n=== fetch errors ===")
        for name, ats, note in errors:
            print("  %-30s %-15s %s" % (name[:30], ats, note))
    print("\nTOTAL open jobs collected via API: %d  (from %d companies)"
          % (len(jobs), len(set(j["company"] for j in jobs))))


if __name__ == "__main__":
    main()
