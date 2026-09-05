#!/usr/bin/env python3
"""Layer 2 — institutional directory: French Tech Aix-Marseille.

`lafrenchtech-aixmarseille.fr` runs WordPress; its startup directory is the
`annuaire` custom post type, exposed unauthenticated at
`/wp-json/wp/v2/annuaire`. We page through it for the company name + public
page, then best-effort resolve a website by following the WP page's outbound
link (the "Voir le site" button).

    python jobboard/annuaire/frenchtech_amp.py                 # -> companies.frenchtech-amp.json
    python jobboard/annuaire/frenchtech_amp.py --no-website    # names only, fast

Output rows are shaped like `companies.json` ({name, domain?, source}) so they
can be concatenated straight into the ATS resolver's input.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html import unescape

BASE = "https://lafrenchtech-aixmarseille.fr/wp-json/wp/v2/annuaire"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SKIP_HOSTS = re.compile(
    r"(lafrenchtech-aixmarseille\.fr|facebook\.|twitter\.|x\.com|linkedin\.|"
    r"instagram\.|youtube\.|wordpress\.|w\.org|gravatar\.|google\.|maps\.|"
    r"apple\.com|schema\.org|gmpg\.org)", re.I)


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace"), dict(r.headers)


def fetch_directory(verbose=True):
    rows, page = [], 1
    while True:
        url = "%s?per_page=100&page=%d&_fields=id,link,title,slug" % (BASE, page)
        try:
            body, _ = _get(url)
        except urllib.error.HTTPError as e:
            if e.code == 400:      # ran past the last page
                break
            raise
        batch = json.loads(body)
        if not batch:
            break
        for e in batch:
            rows.append({
                "name": unescape(re.sub(r"<[^>]+>", "", e["title"]["rendered"]).strip()),
                "ft_page": e["link"],
                "ft_slug": e.get("slug"),
            })
        if verbose:
            print("  page %d  +%d  (total %d)" % (page, len(batch), len(rows)), file=sys.stderr)
        page += 1
        time.sleep(0.2)
    # de-dupe by lowered name
    seen, out = set(), []
    for r in rows:
        k = r["name"].lower()
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


_LINK_RX = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)


def resolve_website(ft_page, timeout=20):
    """Follow the directory page, return the first plausible external domain."""
    try:
        body, _ = _get(ft_page, timeout=timeout)
    except Exception:
        return None
    # the theme wraps the site button in <a class="... site ..."> or a
    # "Voir le site"/"Site web" anchor; fall back to any external link.
    candidates = []
    for m in re.finditer(r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
                         body, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).lower()
        if SKIP_HOSTS.search(href):
            continue
        score = 0
        if re.search(r"site|web|visiter|d.couvrir", label):
            score += 2
        candidates.append((score, href))
    if not candidates:
        for href in _LINK_RX.findall(body):
            if not SKIP_HOSTS.search(href):
                candidates.append((0, href))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    host = re.sub(r"^https?://(www\.)?", "", candidates[0][1]).split("/")[0]
    return host.strip().strip(".").lower() or None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.frenchtech-amp.json"))
    ap.add_argument("--no-website", action="store_true", help="skip per-company website resolution")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args()

    print("Fetching French Tech Aix-Marseille directory ...", file=sys.stderr)
    rows = fetch_directory()
    if args.limit:
        rows = rows[: args.limit]
    print("  %d unique structures" % len(rows), file=sys.stderr)

    if not args.no_website:
        print("Resolving websites (best effort) ...", file=sys.stderr)
        for i, r in enumerate(rows, 1):
            r["domain"] = resolve_website(r["ft_page"])
            if i % 50 == 0:
                got = sum(1 for x in rows[:i] if x.get("domain"))
                print("  %d/%d  (%d with domain)" % (i, len(rows), got), file=sys.stderr)
            time.sleep(args.sleep)

    for r in rows:
        r["source"] = "frenchtech-aix-marseille"

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    got = sum(1 for r in rows if r.get("domain"))
    print("\nwrote %s  (%d companies, %d with a resolved domain)"
          % (args.output, len(rows), got), file=sys.stderr)


if __name__ == "__main__":
    main()
