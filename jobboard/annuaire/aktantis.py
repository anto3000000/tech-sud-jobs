#!/usr/bin/env python3
"""Layer 2 — cluster directory: Aktantis (ex-Pôle SCS).

The Provence-Alpes-Côte d'Azur deeptech pole SCS (Solutions Communicantes
Sécurisées) merged with Optitec in late 2024 and rebranded **Aktantis**
(`pole-scs.org` now 301s to `aktantis.com`). ~400 members: microelectronics,
IoT, AI & data, cybersecurity, photonics.

The member list is a plain paginated WordPress archive at
`/annuaire-des-membres/page/N/`; each card is a `<div class="item itemN"
data-zone data-type data-techno>` whose Bootstrap modal holds the name,
pitch, postal address and `Site Web` link.

    python jobboard/annuaire/aktantis.py                 # -> data/companies.aktantis.json  (PACA members)
    python jobboard/annuaire/aktantis.py --all-regions   # keep Occitanie / hors-région too

Rows: {name, domain?, source, zone, techno, city?}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "https://www.aktantis.com/annuaire-des-membres/"

# data-zone values that sit inside Provence-Alpes-Côte d'Azur (this board's remit).
PACA_ZONES = {"alpes-maritimes", "bouches-du-rhone", "var", "vaucluse",
              "alpes-de-haute-provence", "hautes-alpes", "region-sud"}

_ITEM_SPLIT_RX = re.compile(r'<div class="item item\d+"\s+')
_ATTR_RX = re.compile(r'data-zone="([^"]*)"\s+data-type="([^"]*)"\s+data-techno="([^"]*)"')
_NAME_RX = re.compile(r'<div class="title"><h5>(.*?)</h5>', re.S)
_SITE_RX = re.compile(r'website-item[^>]*>\s*<b>\s*Site Web\s*:\s*</b>\s*<a href="([^"]+)"', re.I)
_ADDR_RX = re.compile(r'<b>\s*Adresse\s*:\s*</b>\s*<br/?>\s*([^<]+)</p>', re.I)


def _parse_page(html):
    rows = []
    for body in _ITEM_SPLIT_RX.split(html)[1:]:
        attr = _ATTR_RX.match(body)
        zone, _typ, techno = attr.groups() if attr else ("", "", "")
        m = _NAME_RX.search(body)
        name = clean_text(m.group(1)) if m else None
        if not name:
            continue
        site = _SITE_RX.search(body)
        dom = norm_domain(site.group(1)) if site and not is_social(site.group(1)) else None
        addr = _ADDR_RX.search(body)
        city = None
        if addr:
            parts = [p.strip() for p in clean_text(addr.group(1)).split(",")]
            # ".. <street>, <City>, <zip>, France" -> take the token before a 4-5 digit zip
            for i, p in enumerate(parts):
                if re.search(r"\b\d{4,5}\b", p) and i:
                    city = re.sub(r"\b\d{4,5}\b", "", parts[i - 1]).strip() or None
                    break
            city = city or (parts[-2] if len(parts) >= 2 else None)
        row = {"name": name, "source": "aktantis", "zone": zone or None,
               "techno": techno or None}
        if dom:
            row["domain"] = dom
        if city:
            row["city"] = city
        rows.append(row)
    return rows


def fetch(paca_only=True, max_pages=40):
    rows, page = [], 1
    while page <= max_pages:
        url = BASE if page == 1 else "%spage/%d/" % (BASE, page)
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001  (404 = ran past the last page)
            print("  stop at page %d (%s)" % (page, e), file=sys.stderr)
            break
        batch = _parse_page(html)
        if not batch:
            break
        rows += batch
        print("  page %d  +%d  (total %d)" % (page, len(batch), len(rows)), file=sys.stderr)
        page += 1
    if paca_only:
        kept = [r for r in rows if (r.get("zone") or "").split(",")[0].strip() in PACA_ZONES]
        print("  %d/%d rows inside PACA" % (len(kept), len(rows)), file=sys.stderr)
        rows = kept
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.aktantis.json"))
    ap.add_argument("--all-regions", action="store_true",
                    help="keep Occitanie / hors-région members (default: PACA only)")
    args = ap.parse_args()

    print("Fetching Aktantis (ex-Pôle SCS) member directory ...", file=sys.stderr)
    rows = dedupe(fetch(paca_only=not args.all_regions))
    write(args.output, rows, "members")


if __name__ == "__main__":
    main()
