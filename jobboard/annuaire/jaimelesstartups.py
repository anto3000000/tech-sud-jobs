#!/usr/bin/env python3
"""Layer 2 — startup directory: J'aime les startups, city page Marseille.

`/startups/villes/marseille/` lists only the first 40 startups: `/page/N/`,
`?paged=` and `?page=` all return the same page and the REST API is locked
(401), so that is the ceiling. The loop still walks pages in case it is fixed
(it stops on the first page with no new card). Cards carry name + fiche URL only; the company website
is the `outline-btn` link on each fiche (``?utm_source=jaimelesstartups``), so
one extra GET per startup (threaded, cached in-process).

    python jobboard/annuaire/jaimelesstartups.py     # -> data/companies.jaimelesstartups.json

Rows: {name, domain?, source, ft_page}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "https://www.jaimelesstartups.fr/startups/villes/%s/"
CARD_RX = re.compile(r'<h3 id="startup-card-\d+-title">\s*<a href="([^"]+)">\s*(.*?)\s*</a>', re.S)
SITE_RX = re.compile(r'<a href="(https?://[^"]+)"[^>]*class="outline-btn"', re.S)


def list_page(city, page):
    url = BASE % city + ("page/%d/" % page if page > 1 else "")
    try:
        return CARD_RX.findall(get(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def site_of(fiche):
    try:
        m = SITE_RX.search(get(fiche))
    except Exception as e:  # noqa: BLE001 - one bad fiche must not sink the run
        print("  ! %s: %s" % (fiche, e), file=sys.stderr)
        return None
    if not m or is_social(m.group(1)):
        return None
    d = norm_domain(m.group(1))
    return d if d and "jaimelesstartups" not in d else None


def fetch(city, max_pages):
    cards, seen = [], set()
    for page in range(1, max_pages + 1):
        batch = [c for c in list_page(city, page) if c[0] not in seen]
        if not batch:
            break
        seen.update(c[0] for c in batch)
        cards += batch
        print("  page %d: %d (total %d)" % (page, len(batch), len(cards)), file=sys.stderr)
    with ThreadPoolExecutor(6) as ex:
        domains = list(ex.map(lambda c: site_of(c[0]), cards))
    rows = []
    for (url, name), dom in zip(cards, domains):
        row = {"name": clean_text(name), "source": "jaimelesstartups", "ft_page": url}
        if dom:
            row["domain"] = dom
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="marseille")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.jaimelesstartups.json"))
    args = ap.parse_args()
    print("Fetching J'aime les startups (%s) ..." % args.city, file=sys.stderr)
    write(args.output, dedupe(fetch(args.city, args.max_pages)), "startups")


if __name__ == "__main__":
    main()
