#!/usr/bin/env python3
"""Concatenate the hand-curated seed list with every layer-2 directory dump
into a single resolver input.

    python jobboard/merge_companies.py            # -> data/companies.all.json

Inputs:
    jobboard/companies.json                       hand-curated (~58, carries `known` overrides)
    jobboard/data/companies.frenchtech-amp.json   French Tech Aix-Marseille  (~660)
    jobboard/data/companies.frenchtech-cotedazur.json  French Tech Côte d'Azur / Sophia-Nice (~100)
    jobboard/data/companies.telecom-valley.json   Telecom Valley cluster, Sophia (~125)
    jobboard/data/companies.aktantis.json         Aktantis / ex-Pôle SCS, PACA deeptech (~275)
    jobboard/data/companies.medinsoft.json        Medinsoft, Marseille/Aix (few — collection barely live)

De-dupe key = normalized registrable domain, then normalized name. On a clash
the earlier source wins (curated first, so its verified `known` block is kept);
annuaire-only rows are appended with their metadata (tags / city / ft_page …)
so `resolve.py` can fingerprint their ATS.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

DEFAULT_ANNUAIRES = [
    os.path.join(DATA, "companies.frenchtech-amp.json"),
    os.path.join(DATA, "companies.frenchtech-cotedazur.json"),
    os.path.join(DATA, "companies.telecom-valley.json"),
    os.path.join(DATA, "companies.aktantis.json"),
    os.path.join(DATA, "companies.medinsoft.json"),
]
# metadata worth carrying into the resolver input (everything else is dropped)
CARRY = ("ft_page", "ft_slug", "tags", "note", "city", "zone", "techno")


def norm_domain(d):
    if not d:
        return None
    d = str(d).strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    d = re.sub(r"^www\.", "", d).strip(". ")
    return d or None


def norm_name(n):
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", n.lower())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--curated", default=os.path.join(HERE, "companies.json"))
    ap.add_argument("--annuaire", nargs="*", default=None,
                    help="directory dumps to merge (default: the 5 known layer-2 files)")
    ap.add_argument("-o", "--output", default=os.path.join(DATA, "companies.all.json"))
    args = ap.parse_args()

    curated = json.load(open(args.curated, encoding="utf-8"))
    annuaire_paths = args.annuaire if args.annuaire is not None else DEFAULT_ANNUAIRES

    seen_dom, seen_name = set(), set()
    out = []
    for c in curated:
        c.setdefault("source", "curated")
        d = norm_domain(c.get("domain"))
        if d:
            c["domain"] = d
            seen_dom.add(d)
        seen_name.add(norm_name(c.get("name")))
        out.append(c)

    per_source = {}
    for path in annuaire_paths:
        if not os.path.exists(path):
            print("skip (missing): %s" % path, file=sys.stderr)
            continue
        rows = json.load(open(path, encoding="utf-8"))
        added = dupes = 0
        for a in rows:
            d = norm_domain(a.get("domain"))
            nm = norm_name(a.get("name"))
            if (d and d in seen_dom) or (nm and nm in seen_name):
                dupes += 1
                continue
            src = a.get("source") or os.path.basename(path)
            row = {"name": a.get("name"), "source": src}
            if d:
                row["domain"] = d
                seen_dom.add(d)
            if nm:
                seen_name.add(nm)
            for k in CARRY:
                if a.get(k):
                    row[k] = a[k]
            out.append(row)
            added += 1
        per_source[os.path.basename(path)] = (len(rows), added, dupes)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("curated: %d" % len(curated))
    for name, (total, added, dupes) in per_source.items():
        print("  %-38s %4d rows  ->  +%-4d new  (%d dupes)" % (name, total, added, dupes))
    with_dom = sum(1 for r in out if r.get("domain"))
    print("wrote %s  (%d companies, %d with a domain)" % (args.output, len(out), with_dom))


if __name__ == "__main__":
    main()
