#!/usr/bin/env python3
"""Resolve the real ATS + slug for every company (recos #1 & #2).

    python jobboard/resolve.py                       # all companies
    python jobboard/resolve.py --only 10             # first 10
    python jobboard/resolve.py --no-page             # direct-probe pass only (fast)
    python jobboard/resolve.py -i companies.json -o companies.resolved.json

Writes <output> (JSON) and prints a summary table. The resolved file is the
corrected version of the hand-written `ats` / `slug` columns.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jobboard.ats.resolver import resolve_all  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", default=os.path.join(HERE, "companies.json"))
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "companies.resolved.json"))
    ap.add_argument("--only", type=int, default=0, help="limit to first N companies")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-direct", action="store_true", help="skip hosted-board probing")
    ap.add_argument("--no-page", action="store_true", help="skip careers-page fingerprinting")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        companies = json.load(f)
    if args.only:
        companies = companies[: args.only]

    print("Resolving %d companies (workers=%d, direct=%s, page=%s)..."
          % (len(companies), args.workers, not args.no_direct, not args.no_page), file=sys.stderr)

    resolved = resolve_all(
        companies, workers=args.workers,
        do_direct=not args.no_direct, do_page=not args.no_page,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(resolved, f, ensure_ascii=False, indent=2)

    _print_table(resolved)
    _print_stats(resolved)
    print("\nwrote %s" % args.output, file=sys.stderr)


def _print_table(resolved):
    hdr = ("COMPANY", "DECLARED", "RESOLVED", "SLUG", "METHOD", "VIA", "CONF", "RVW")
    rows = [hdr]
    for r in resolved:
        rows.append((
            r["name"][:26],
            (r["declared_ats"] or "-")[:12],
            (r["resolved_ats"] or "-")[:18],
            (r["resolved_slug"] or "-")[:22],
            (r["method"] or "-")[:7],
            (r["resolved_via"] or "-")[:22],
            r["confidence"][:8],
            "!" if r.get("needs_review") else "",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(hdr))]
    for i, row in enumerate(rows):
        line = "  ".join(c.ljust(widths[j]) for j, c in enumerate(row))
        print(line)
        if i == 0:
            print("  ".join("-" * w for w in widths))


def _print_stats(resolved):
    from collections import Counter
    by_ats = Counter(r["resolved_ats"] for r in resolved)
    by_via = Counter(r["resolved_via"] for r in resolved)
    by_method = Counter(r["method"] for r in resolved)
    print("\n--- resolved ATS ---")
    for k, v in by_ats.most_common():
        print("  %-18s %d" % (k, v))
    print("--- resolved via ---")
    for k, v in by_via.most_common():
        print("  %-18s %d" % (k, v))
    print("--- fetch method ---")
    for k, v in by_method.most_common():
        print("  %-18s %d" % (k, v))
    changed = sum(1 for r in resolved
                  if r["resolved_ats"] and r["declared_ats"]
                  and r["resolved_ats"] != r["declared_ats"])
    review = [r["name"] for r in resolved if r.get("needs_review")]
    print("\n%d / %d companies had the declared ATS corrected." % (changed, len(resolved)))
    if review:
        print("needs_review (%d): %s" % (len(review), ", ".join(review)))


if __name__ == "__main__":
    main()
