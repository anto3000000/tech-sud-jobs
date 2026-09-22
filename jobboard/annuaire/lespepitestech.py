#!/usr/bin/env python3
"""Layer 2 — startup directory: Les Pépites Tech, city page Marseille.

`/startup/marseille?page=N` (Drupal, 0-indexed, ~10 cards per page, ~18 pages).
Each card links to a fiche `/startup-de-la-french-tech/<slug>` whose website
link is tagged ``?utm_source=LesPepitesTech.com``; one GET per fiche (threaded).
Cards also carry a `#tag` (saas, fintech ...) and a summary.

    python jobboard/annuaire/lespepitestech.py       # -> data/companies.lespepitestech.json

Rows: {name, domain?, source, tags[], ft_page}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORIGIN = "https://lespepitestech.com"
CARD_RX = re.compile(
    r'<article class="startup-entry lpt-card.*?href="(/startup-de-la-french-tech/[^"]+?)\s*".*?'
    r'<h3>(.*?)</h3>(.*?)</article>', re.S)
TAG_RX = re.compile(r'href="/startup-collection/[^"]+">\s*#([^<\s]+)')
UTM_RX = re.compile(r'href="(https?://[^"]+utm_source=LesPepitesTech\.com[^"]*)"', re.I)


def list_page(city, page):
    cards = []
    for path, name, rest in CARD_RX.findall(get("%s/startup/%s?page=%d" % (ORIGIN, city, page))):
        cards.append((path.strip(), clean_text(name), TAG_RX.findall(rest)))
    return cards


def site_of(path):
    try:
        m = UTM_RX.search(get(ORIGIN + path))
    except Exception as e:  # noqa: BLE001
        print("  ! %s: %s" % (path, e), file=sys.stderr)
        return None
    if not m or is_social(m.group(1)):
        return None
    return norm_domain(m.group(1))


def fetch(city, max_pages):
    cards, seen = [], set()
    for page in range(max_pages):
        batch = [c for c in list_page(city, page) if c[0] not in seen]
        if not batch:
            break
        seen.update(c[0] for c in batch)
        cards += batch
        print("  page %d: %d (total %d)" % (page, len(batch), len(cards)), file=sys.stderr)
    with ThreadPoolExecutor(6) as ex:
        domains = list(ex.map(lambda c: site_of(c[0]), cards))
    rows = []
    for (path, name, tags), dom in zip(cards, domains):
        row = {"name": name, "source": "lespepitestech", "ft_page": ORIGIN + path}
        if dom:
            row["domain"] = dom
        if tags:
            row["tags"] = tags
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="marseille")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.lespepitestech.json"))
    args = ap.parse_args()
    print("Fetching Les Pépites Tech (%s) ..." % args.city, file=sys.stderr)
    write(args.output, dedupe(fetch(args.city, args.max_pages)), "startups")


if __name__ == "__main__":
    main()
