#!/usr/bin/env python3
"""Layer 2 — cluster directory: Medinsoft (Marseille / Aix éditeurs & ESN).

Medinsoft's site is Wix. Its public directory (`/annuaires`) is backed by a Wix
Data collection called ``Annuaire`` whose rows are pre-rendered into the page's
``<script id="wix-warmup-data">`` blob — no API call, no auth needed.

⚠️  The public collection is barely populated: it launched in Dec-2024 and holds
only a handful of rows (the association itself has ~160 members — those just
aren't published here yet). This scraper harvests whatever is live; re-run it
periodically. If Medinsoft ever exposes the full list, the warmup blob's
``datasetSize.total`` will jump and the parsing below still holds.

    python jobboard/annuaire/medinsoft.py               # -> data/companies.medinsoft.json

Rows: {name, domain?, source, city?, tags[]}. Feed into merge_companies.py.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import dedupe, get, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = "https://www.medinsoft.com/annuaires"
_WARMUP_RX = re.compile(r'<script[^>]*id="wix-warmup-data"[^>]*>(.*?)</script>', re.S)


def _collection_rows(warmup):
    store = (warmup.get("appsWarmupData", {}).get("dataBinding", {})
             .get("dataStore", {}).get("recordsByCollectionId", {}))
    for coll, recs in store.items():
        if coll.lower().startswith("annuaire"):
            return list(recs.values())
    return []


def parse(html):
    m = _WARMUP_RX.search(html)
    if not m:
        return []
    warmup = json.loads(m.group(1))
    rows = []
    for rec in _collection_rows(warmup):
        name = (rec.get("title") or "").strip()
        if not name:
            continue
        row = {"name": name, "source": "medinsoft"}
        dom = norm_domain(rec.get("siteInternet"))
        if dom:
            row["domain"] = dom
        city = (rec.get("address") or {}).get("city")
        if city:
            row["city"] = city
        tags = [t for t in (rec.get("typeDeStructure1") or []) if t]
        if tags:
            row["tags"] = tags
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.medinsoft.json"))
    ap.add_argument("--html-file", help="parse a locally-saved copy of /annuaires instead of fetching")
    args = ap.parse_args()

    if args.html_file:
        html = open(args.html_file, encoding="utf-8").read()
    else:
        print("Fetching Medinsoft directory ...", file=sys.stderr)
        html = get(PAGE)

    rows = dedupe(parse(html))
    if not rows:
        print("WARNING: 0 rows — warmup blob missing or collection empty", file=sys.stderr)
    write(args.output, rows, "members")


if __name__ == "__main__":
    main()
