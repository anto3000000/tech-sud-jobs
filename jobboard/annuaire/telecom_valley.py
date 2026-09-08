#!/usr/bin/env python3
"""Layer 2 — cluster directory: Telecom Valley (Sophia Antipolis).

`telecom-valley.fr` runs WordPress; every member is a `project` custom post,
exposed unauthenticated at `/wp-json/wp/v2/project`. ~140 members, each fiche
carries the company website as plain text in the excerpt ("... www.acme.fr
Activité ...") and as a link in the Divi body; `project_category` terms give
territory (Sophia Antipolis / Nice / Cannes / Var ...), structure (Startup /
PME / Grands groupes) and a business tag (FinTech, IoT & Systèmes embarqués ...).

    python jobboard/annuaire/telecom_valley.py            # -> data/companies.telecom-valley.json
    python jobboard/annuaire/telecom_valley.py --keep-partners   # keep the "Partenaires" institutions

Rows: {name, domain?, source, tags[], ft_page}. Feed into merge_companies.py.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import clean_text, dedupe, get_json, is_social, norm_domain, write  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API = "https://www.telecom-valley.fr/wp-json/wp/v2"

# `project_category` terms that mark a row as a non-company partner / institution.
PARTNER_TERMS = {"Partenaires", "Institutionnel", "Financeur", "Média", "Presse"}


def category_map():
    out = {}
    for page in (1, 2, 3):
        batch = get_json("%s/project_category?per_page=100&page=%d" % (API, page))
        if not batch:
            break
        for term in batch:
            out[term["id"]] = clean_text(term["name"])
    return out


# excerpt text often glues the URL to the next word ("www.acme.frActivité"),
# so bound the match with an explicit TLD list + "not followed by a letter".
_TLD = (r"fr|com|io|ai|net|org|eu|tech|co|dev|app|cloud|xyz|shop|store|re|nc|"
        r"pro|paris|group|team|agency|studio|digital|solutions|company|world")
_EXCERPT_DOM_RX = re.compile(
    r"(?:https?://)?(www\.[a-z0-9-]+(?:\.[a-z0-9-]+)*?\.(?:%s))(?![a-z])" % _TLD, re.I)


def _domain_from_excerpt(text):
    # the theme prints "Contact <adresse> www.example.fr Activité <pitch>"
    m = _EXCERPT_DOM_RX.search(text)
    return norm_domain(m.group(1)) if m else None


def _domain_from_body(html):
    for href in re.findall(r'href=["\'](https?://[^"\']+)["\']', html):
        if is_social(href) or "telecom-valley.fr" in href or "/wp-content/" in href:
            continue
        if re.search(r"\.(pdf|jpe?g|png|zip)(\?|$)", href, re.I):
            continue
        d = norm_domain(href)
        if d and "telecom-valley" not in d:
            return d
    return None


def fetch(keep_partners=False):
    cats = category_map()
    print("  %d project_category terms" % len(cats), file=sys.stderr)
    rows, page, pages = [], 1, 1
    while page <= pages:
        batch = get_json(
            "%s/project?per_page=100&page=%d&_fields=id,slug,link,title,excerpt,content,project_category"
            % (API, page))
        for p in batch:
            name = clean_text(p["title"]["rendered"])
            tags = [cats.get(i) for i in p.get("project_category", []) if cats.get(i)]
            if not keep_partners and (set(tags) & PARTNER_TERMS) and len(tags) <= 2:
                continue
            dom = (_domain_from_excerpt(clean_text(p["excerpt"]["rendered"]))
                   or _domain_from_body(p["content"]["rendered"]))
            row = {"name": name, "source": "telecom-valley", "ft_page": p["link"]}
            if dom:
                row["domain"] = dom
            if tags:
                row["tags"] = tags
            rows.append(row)
        # X-WP-TotalPages isn't exposed by get(); infer from a short page
        pages = page + 1 if len(batch) == 100 else page
        page += 1
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "companies.telecom-valley.json"))
    ap.add_argument("--keep-partners", action="store_true",
                    help="keep rows tagged only Partenaires / Institutionnel / Média")
    args = ap.parse_args()

    print("Fetching Telecom Valley member directory ...", file=sys.stderr)
    rows = dedupe(fetch(keep_partners=args.keep_partners))
    write(args.output, rows, "members")


if __name__ == "__main__":
    main()
