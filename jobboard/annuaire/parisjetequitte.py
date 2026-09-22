#!/usr/bin/env python3
"""Layer 2 — editorial list: Paris, je te quitte, "Startups à Marseille".

The article ends with a `<ul>` of "Category : Name, Name, ..." lines (~30
names, no websites — the resolver falls back to name-based probing).

    python jobboard/annuaire/parisjetequitte.py     # -> data/companies.parisjetequitte.json

Rows: {name, source, tags[], ft_page}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
URL = "https://paris-jetequitte.com/startups-marseille/"


def fetch(url):
    html = get(url)
    i = html.find("Notre liste complète")
    section = html[i:html.find("</ul>", i)]
    rows = []
    for li in re.findall(r"<li>(.*?)</li>", section, re.S):
        cat, _, names = clean_text(li).partition(" : ")
        for name in names.split(","):
            name = name.strip()
            if name:
                rows.append({"name": name, "source": "parisjetequitte", "tags": [cat], "ft_page": url})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.parisjetequitte.json"))
    args = ap.parse_args()
    print("Fetching Paris, je te quitte list ...", file=sys.stderr)
    write(args.output, dedupe(fetch(args.url)), "startups")


if __name__ == "__main__":
    main()
