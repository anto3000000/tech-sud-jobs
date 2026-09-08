#!/usr/bin/env python3
"""Layer 2 — institutional directory: French Tech Côte d'Azur (Sophia / Nice).

`frenchtechcotedazur.fr` runs WordPress (Salient). Its members live in a
`portfolio` custom post type that is **not** exposed over the REST API, but the
public page *Nos Start-up et Scale-up* renders them as a Nectar post-grid where
every card links straight to the company website — exactly the
``{name, domain}`` pair the ATS resolver needs.

That grid is capped at 100 items (of ~208 portfolio entries, shown A→Z); the
~108 in the tail are reachable via the ugly-permalink archive
``/?post_type=portfolio&paged=N`` but most of those fiches carry no website
link, so they'd only feed the resolver blind name-guesses. We take the 100
that come with a real domain.

    python jobboard/annuaire/frenchtech_cotedazur.py     # -> data/companies.frenchtech-cotedazur.json

Historically this host 403'd a bare ``curl``; a browser UA (see _common.py) is
enough now. If it starts 403ing from CI, fetch the page once via a headless
browser and pass it with ``--html-file``.

Rows: {name, domain?, source, note?}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = "https://www.frenchtechcotedazur.fr/nos-start-up-et-scale-up-french-tech-cote-dazur/"

_ITEM_SPLIT_RX = re.compile(r'<div class="nectar-post-grid-item"')
_NAME_RX = re.compile(r'<h3 class="post-heading">.*?<span>(.*?)</span>', re.S)
_EXCERPT_RX = re.compile(r'<span class="meta-excerpt">(.*?)</span>', re.S)
_HREF_RX = re.compile(r'href="(https?://[^"]+)"')


def parse(html):
    rows = []
    for block in _ITEM_SPLIT_RX.split(html)[1:]:
        m = _NAME_RX.search(block)
        name = clean_text(m.group(1)) if m else None
        if not name:
            continue
        dom = None
        for href in _HREF_RX.findall(block):
            if is_social(href) or "frenchtechcotedazur.fr" in href:
                continue
            dom = norm_domain(href)
            if dom:
                break
        ex = _EXCERPT_RX.search(block)
        row = {"name": name, "source": "frenchtech-cote-dazur"}
        if dom:
            row["domain"] = dom
        note = clean_text(ex.group(1)) if ex else ""
        if note:
            row["note"] = note
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.frenchtech-cotedazur.json"))
    ap.add_argument("--html-file", help="parse a locally-saved copy of the directory page instead of fetching")
    args = ap.parse_args()

    if args.html_file:
        html = open(args.html_file, encoding="utf-8").read()
    else:
        print("Fetching French Tech Côte d'Azur directory ...", file=sys.stderr)
        html = get(PAGE)

    rows = dedupe(parse(html))
    if not rows:
        sys.exit("no members parsed — page markup changed, or fetch was blocked (try --html-file)")
    write(args.output, rows, "members")


if __name__ == "__main__":
    main()
