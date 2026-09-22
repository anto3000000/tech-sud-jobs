#!/usr/bin/env python3
"""Layer 2 — incubator directory: Marseille Innovation (French Tech Aix-Marseille).

`marseille-innov.org` is WordPress; every incubated startup is a post in the
`Start-up` category (id 612, ~299), exposed at `/wp-json/wp/v2/posts`. The list
carries only the name + fiche URL; the company site is the first non-social
external link in the fiche body, so one GET per startup (threaded).

    python jobboard/annuaire/marseille_innovation.py   # -> data/companies.marseille-innovation.json

Rows: {name, domain?, source, ft_page}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, get_json, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API = "https://www.marseille-innov.org/wp-json/wp/v2"
CATEGORY = 612  # "Start-up"


def list_posts():
    out, page = [], 1
    while True:
        batch = get_json("%s/posts?categories=%d&per_page=100&page=%d&_fields=link,title"
                         % (API, CATEGORY, page))
        out += [(p["link"], clean_text(p["title"]["rendered"])) for p in batch]
        if len(batch) < 100:
            return out
        page += 1


def site_of(fiche):
    try:
        html = get(fiche)
    except Exception as e:  # noqa: BLE001 - one bad fiche must not sink the run
        print("  ! %s: %s" % (fiche, e), file=sys.stderr)
        return None
    for href in re.findall(r'href=["\'](https?://[^"\']+)["\']', html):
        if is_social(href) or "marseille-innov" in href or "/wp-content/" in href:
            continue
        d = norm_domain(href)
        if d:
            return d
    return None


def fetch():
    posts = list_posts()
    print("  %d startup posts" % len(posts), file=sys.stderr)
    with ThreadPoolExecutor(6) as ex:
        domains = list(ex.map(lambda p: site_of(p[0]), posts))
    rows = []
    for (link, name), dom in zip(posts, domains):
        row = {"name": name, "source": "marseille-innovation", "ft_page": link}
        if dom:
            row["domain"] = dom
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.marseille-innovation.json"))
    args = ap.parse_args()
    print("Fetching Marseille Innovation startups ...", file=sys.stderr)
    write(args.output, dedupe(fetch()), "startups")


if __name__ == "__main__":
    main()
