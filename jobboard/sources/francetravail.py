#!/usr/bin/env python3
"""Layer 1c — France Travail (ex-Pôle Emploi) "Offres d'emploi v2" API.

The official public API: ~300 k live offers, free, filterable by département +
ROME. We sweep the six PACA départements for the *informatique & télécoms*
family (`grandDomaine=M18`), keep only Tech/Data/Product roles via the shared
title classifier, and drop the interim / staffing agencies that mass-republish.

Unlike the ATS layer this is a *job* source, not a *company* source: we get the
posting (and its apply link) directly, so there is no slug to guess and no
homonym risk.

    python jobboard/sources/francetravail.py                 # -> data/francetravail_paca.json
    python jobboard/sources/francetravail.py --all           # every role, no classifier
    python jobboard/sources/francetravail.py --rome M1805,M1806,M1810
    python jobboard/sources/francetravail.py --publiee-depuis 7
    python jobboard/sources/francetravail.py --keep-agencies --keep-adjacent

Credentials (create an app on https://francetravail.io):
    env  FT_CLIENT_ID / FT_CLIENT_SECRET
    or   jobboard/.env   with  FT_CLIENT_ID=...  / FT_CLIENT_SECRET=...
    or   --client-id / --client-secret
Scope required on the app: "Offres d'emploi v2".
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from jobboard.classify import classify  # noqa: E402

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

PACA_DEPTS = ["04", "05", "06", "13", "83", "84"]
GRAND_DOMAINE = "M18"           # Systèmes d'information et de télécommunication
PAGE = 150                      # API hard max per request
TOTAL_CAP = 3149               # API hard max offset

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# interim / staffing / RPO shops: they re-publish the same client roles across
# every board and drown out the actual employers.
AGENCY_RX = re.compile(
    r"\b(proman|iziwork|adecco|manpower|randstad|synergie|expectra|crit\b|"
    r"gi group|actual\b|temporis|supplay|start people|kelly services|"
    r"page personnel|hays\b|fed it|fed group|mistertemp|aprojob|r\.?a\.?s\b|"
    r"groupe morgan|lynx rh|abalone|aquila rh|gerinter|interaction|leader interim|"
    r"menway|triangle interim|job link|sbc interim|domino rh|ergalis|partnaire|"
    r"apf entreprises|walters people|robert half|approach people|nextep|"
    r"external recruitment|lhh recruitment|talent\.io|urban linker|seydoo|"
    r"le mercato de l|rh developpement|rh developpe|forums? talents? handicap|"
    r"cabinet .*recrut|recrut.* cabinet|talent.?up|hunteed|talentueux|silkhom|"
    r"\bltd\b|h consulting|externatic|seyos|approach people|clementine\b|"
    r"lincoln hr|hemera|kicklox|freelance\.com|comet\b|malt community|"
    r"harry hope|hays|michael page|hudson|robert walters|fed group|"
    r"grafton|nigel frank|austin bright|walters people|morgan philips|"
    r"substance\b|kaia consulting|\bgif\b|aabm|rh partners|talents? handicap)\b",
    re.I)

# ESN / SSII / conseil: legit employers, but they mass-post generic "Consultant
# <techno>" roles for client missions. WTTJ already covers the product companies,
# so on a PACA-tech-*employer* board these mostly add noise. --keep-agencies
# lets them back in.
ESN_RX = re.compile(
    r"\b(cleeven|klanik|davidson|metsys|synanto|sea tpi|astridsen|akkodis|"
    r"alten\b|alten sud|capgemini|sopra steria|sogeti|atos\b|eviden|inetum|"
    r"\bsii\b|sii sud|expleo|ausy|assystem|segula|alter solutions|talan\b|"
    r"devoteam|neurones|umanis|hardis|sqli\b|astek|cs group|scalian|"
    r"orange business|econocom|micropole|keyrus|business & decision|"
    r"sword group|niji\b|magellan|mc2i|wavestone|onepoint|open groupe|"
    r"consort|infotel|neo soft|neosoft|cga\b| classe internationale|"
    r"softeam|hn services|acensi|ineat|axiome|viseo|smile\b|sfeir|"
    r"positive way|extia|ineo|nexio|apside|asubay| redlab|nform|"
    r"informatique industrielle|geser|pragmait|inop.?team|inov team)\b",
    re.I)

_CLEAN_TITLE = re.compile(
    r"\s*[\(\-–—/]?\s*\b([hf]\s*/\s*[hfx]|[fh]\s*/\s*[hf]\s*/\s*x|m\s*/\s*f|w\s*/\s*m)\b\s*\)?\s*$",
    re.I)
_SAL_DOT0 = re.compile(r"(\d)\.0+\b")


# --------------------------------------------------------------------------- #
#  credentials
# --------------------------------------------------------------------------- #
def load_env_file(path=os.path.join(ROOT, "jobboard", ".env")):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_creds(cli_id, cli_secret):
    envf = load_env_file()
    cid = cli_id or os.environ.get("FT_CLIENT_ID") or envf.get("FT_CLIENT_ID")
    sec = cli_secret or os.environ.get("FT_CLIENT_SECRET") or envf.get("FT_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("error: France Travail credentials missing — set FT_CLIENT_ID / "
                 "FT_CLIENT_SECRET (env, jobboard/.env, or --client-id/--client-secret)")
    return cid, sec


# --------------------------------------------------------------------------- #
#  http
# --------------------------------------------------------------------------- #
def get_token(client_id, client_secret, timeout=30):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tok = json.loads(r.read().decode("utf-8", "replace"))
    return tok["access_token"], int(tok.get("expires_in", 1200))


def search_page(token, departement, start, rome=None, grand_domaine=None,
                publiee_depuis=None, timeout=30):
    """One /offres/search call. Returns (list_of_offres, total)."""
    params = {"departement": departement, "range": "%d-%d" % (start, start + PAGE - 1)}
    if rome:
        params["codeROME"] = rome
    elif grand_domaine:
        params["grandDomaine"] = grand_domaine
    if publiee_depuis:
        params["publieeDepuis"] = str(publiee_depuis)
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 204:
                    return [], 0
                data = json.loads(r.read().decode("utf-8", "replace"))
                total = _content_range_total(r.headers.get("Content-Range"))
                return data.get("resultats", []) or [], total
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return [], 0
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise SystemExit("error: FT search %s -> HTTP %s %s\n  %s"
                             % (params, e.code, e.reason, body))
    return [], 0


def _content_range_total(hdr):
    m = re.search(r"/(\d+)\s*$", hdr or "")
    return int(m.group(1)) if m else 0


def sweep(token, depts, rome=None, grand_domaine=None, publiee_depuis=None,
          sleep=0.25, verbose=True):
    seen, out = set(), []
    for dep in depts:
        start, total = 0, None
        while True:
            batch, tot = search_page(token, dep, start, rome=rome,
                                     grand_domaine=grand_domaine,
                                     publiee_depuis=publiee_depuis)
            if total is None:
                total = tot
            for o in batch:
                oid = o.get("id")
                if oid and oid not in seen:
                    seen.add(oid)
                    out.append(o)
            if verbose:
                print("  dep %s  %d-%d  (+%d)  total=%s"
                      % (dep, start, start + PAGE - 1, len(batch), total), file=sys.stderr)
            start += PAGE
            if not batch or start >= min(total or 0, TOTAL_CAP):
                break
            time.sleep(sleep)
    return out


# --------------------------------------------------------------------------- #
#  normalize
# --------------------------------------------------------------------------- #
def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return s


def _city_from_libelle(lib):
    # "13 - MARSEILLE 01" -> "Marseille 01" ; "06 - NICE" -> "Nice"
    m = re.match(r"\s*\d{2,3}\s*-\s*(.+)$", lib or "")
    city = (m.group(1) if m else (lib or "")).strip()
    if city.isupper() or city.islower():
        city = city.title()
    return city or None


def _apply_url(offre):
    oo = offre.get("origineOffre") or {}
    if oo.get("origine") == "2":
        for p in (oo.get("partenaires") or []):
            if p.get("url"):
                return p["url"]
    if oo.get("urlOrigine"):
        return oo["urlOrigine"]
    return "https://candidat.francetravail.fr/offres/recherche/detail/%s" % offre.get("id")


# --------------------------------------------------------------------------- #
#  apply-link quality  (regie / cabinet offers come in via job aggregators;
#  a real employer posting links to its own ATS)
# --------------------------------------------------------------------------- #
# job boards / aggregators / the France Travail fallback itself
AGGREGATOR_RX = re.compile(
    r"(^|\.)(meteojob\.com|hellowork\.com|jobposting\.pro|lindustrie-recrute\.fr|"
    r"indeed\.com|monster\.fr|keljob\.com|cadremploi\.fr|apec\.fr|optioncarriere\.com|"
    r"jobijoba\.com|jobrapido\.com|talent\.com|neuvoo\.|glassdoor\.|linkedin\.com|"
    r"stepstone\.|regionsjob\.com|emploi-collectivites\.fr|jobteaser\.com|"
    r"clicandpower\.fr|jobtransport\.com|jobindustrie\.com|distrijob\.fr|"
    r"lesjeudis\.com|choosemycompany\.com|figaro-emploi\.|ouestfrance-emploi\.com|"
    r"francetravail\.fr|pole-emploi\.fr|candidat\.francetravail\.fr|"
    r"emploi-environnement\.com|carrieres-publiques\.com|emploipublic\.fr|"
    r"dogfinance\.com|jobvitae\.fr|labonnealternance\.|apec\.|wizbii\.com|"
    r"jobteaser\.|meteojob\.|profilculture\.com|jobtech\.fr|turnover-it\.com|"
    r"free-work\.com|lecoinjobs\.|joblift\.|jooble\.org)",
    re.I)

# recognised ATS / recruiting-software hosts = a direct employer board
KNOWN_ATS_RX = re.compile(
    r"(^|\.)(softy\.pro|beetween\.com|join\.com|teamtailor\.com|welcometothejungle\.com|"
    r"lever\.co|greenhouse\.io|boards\.greenhouse|ashbyhq\.com|jobs\.ashbyhq|"
    r"workable\.com|recruitee\.com|smartrecruiters\.com|taleez\.com|personio\.(com|de|fr)|"
    r"factorialhr\.com|factorial\.(fr|es|com)|flatchr\.io|digitalrecruiters\.com|jobaffinity\.fr|"
    r"mytalentplug\.com|softgarden\.(io|de)|jobvite\.com|myworkdayjobs\.com|aplitrak\.com|"
    r"icims\.com|taleo\.net|cornerstoneondemand\.com|eolia-software|zohorecruit\.|"
    r"gestmax\.fr|cvmanager\.fr|wittyfit|wink-lab\.com|jobology\.fr|talentplug\.com|"
    r"careers\.[a-z0-9-]+|recrutement\.[a-z0-9-]+\.(fr|com)|carriere[s]?\.[a-z0-9-]+\.(fr|com)|"
    r"talent\.[a-z0-9-]+\.(fr|com)|jobs?\.[a-z0-9-]+\.(fr|com|io))",
    re.I)


def _host(url):
    m = re.match(r"https?://([^/]+)", url or "", re.I)
    return (m.group(1).lower() if m else "").lstrip(".")


def link_kind(url):
    """'ats' (direct employer board) | 'aggregator' | 'other'."""
    h = _host(url)
    if not h:
        return "other"
    if KNOWN_ATS_RX.search(h):
        return "ats"
    if AGGREGATOR_RX.search(h):
        return "aggregator"
    return "other"


REMOTE_RX = re.compile(r"t[ée]l[ée]travail|remote|100\s*%\s*distanciel|full\s*remote", re.I)


def normalize(offre, category):
    lt = offre.get("lieuTravail") or {}
    ent = offre.get("entreprise") or {}
    cp = (lt.get("codePostal") or "").strip()
    dep = cp[:2] if len(cp) >= 2 else (re.match(r"\s*(\d{2,3})", lt.get("libelle") or "") or [None, None])[1]
    title = _CLEAN_TITLE.sub("", (offre.get("intitule") or "").strip()).strip(" -–—/")
    sal = ((offre.get("salaire") or {}).get("libelle") or "").strip()
    sal = _SAL_DOT0.sub(r"\1", sal) or None
    desc = offre.get("description") or ""
    city = _city_from_libelle(lt.get("libelle"))
    return {
        "id": "francetravail:%s" % offre.get("id"),
        "title": title,
        "company": (ent.get("nom") or "").strip() or "Entreprise non précisée",
        "company_slug": _slug(ent.get("nom")),
        "city": city,
        "cities": [city] if city else [],
        "department": dep,
        "region": "Provence-Alpes-Côte d'Azur",
        "contract": offre.get("typeContratLibelle") or offre.get("typeContrat"),
        "remote": "télétravail" if REMOTE_RX.search(title + " " + desc[:400]) else None,
        "category": category,
        "profession": offre.get("romeLibelle"),
        "salary": sal,
        "experience_min_years": None,
        "published_at": offre.get("dateCreation"),
        "url": _apply_url(offre),
        "source": "francetravail",
    }


ANON_RX = re.compile(r"^\s*(entreprise\s+non\s+pr[ée]cis[ée]e?|non\s+pr[ée]cis[ée]e?|"
                      r"confidentiel|n/?a)\s*$", re.I)


def run(offres, tech_only=True, keep_adjacent=True, drop_agencies=True, drop_anon=True,
        links="direct", max_per_company=5):
    """links: 'direct' keep only ATS-hosted apply links (drop aggregators + unknown
    hosts), 'no-aggregator' keep ATS + unknown but drop known aggregators, 'any'.
    max_per_company: cap rows per employer (0 = no cap)."""
    kept, kept_cat = [], {}
    st = {"anon": 0, "agency": 0, "outside": 0, "nontech": 0,
          "aggregator": 0, "unknown_link": 0, "capped": 0}
    other_hosts = {}
    for o in offres:
        ent = (o.get("entreprise") or {}).get("nom") or ""
        if drop_anon and (not ent.strip() or ANON_RX.match(ent)):
            st["anon"] += 1
            continue
        if drop_agencies and (AGENCY_RX.search(ent) or ESN_RX.search(ent)):
            st["agency"] += 1
            continue
        cp = ((o.get("lieuTravail") or {}).get("codePostal") or "")[:2]
        if cp and cp not in PACA_DEPTS:
            st["outside"] += 1
            continue
        cat = classify(o.get("intitule"), o.get("romeLibelle"))
        if tech_only and (cat is None or (cat == "tech-adjacent" and not keep_adjacent)):
            st["nontech"] += 1
            continue
        row = normalize(o, cat)
        kind = link_kind(row["url"])
        if links != "any":
            if kind == "aggregator":
                st["aggregator"] += 1
                continue
            if kind == "other":
                other_hosts[_host(row["url"])] = other_hosts.get(_host(row["url"]), 0) + 1
                if links == "direct":
                    st["unknown_link"] += 1
                    continue
        if cat:
            kept_cat[cat] = kept_cat.get(cat, 0) + 1
        kept.append(row)

    kept.sort(key=lambda j: j.get("published_at") or "", reverse=True)

    if max_per_company:
        seen, capped = {}, []
        for j in kept:
            k = j.get("company_slug") or j.get("company")
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > max_per_company:
                st["capped"] += 1
                continue
            capped.append(j)
        kept = capped

    return kept, st, kept_cat, other_hosts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output",
                    default=os.path.join(ROOT, "jobboard", "data", "francetravail_paca.json"))
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    ap.add_argument("--rome", help="comma-separated ROME codes instead of grandDomaine=M18")
    ap.add_argument("--publiee-depuis", type=int, default=None,
                    help="only offers published in the last N days (max 31); default: all active")
    ap.add_argument("--all", action="store_true", help="keep every role, skip the classifier")
    ap.add_argument("--keep-adjacent", action="store_true", default=True)
    ap.add_argument("--no-adjacent", dest="keep_adjacent", action="store_false")
    ap.add_argument("--keep-agencies", dest="drop_agencies", action="store_false", default=True,
                    help="don't filter out interim / staffing agencies + ESN / SSII")
    ap.add_argument("--keep-anonymous", dest="drop_anon", action="store_false", default=True,
                    help="keep offers with no named employer ('Entreprise non précisée')")
    ap.add_argument("--links", choices=["direct", "no-aggregator", "any"], default="direct",
                    help="direct: only apply links on a known ATS host (default); "
                         "no-aggregator: ATS + unknown hosts, drop job-board aggregators; "
                         "any: keep every link")
    ap.add_argument("--max-per-company", type=int, default=5,
                    help="cap rows per employer (regie/ESN spam guard; 0 = no cap)")
    ap.add_argument("--raw-out", help="also dump the untouched API offers here")
    args = ap.parse_args()

    cid, sec = resolve_creds(args.client_id, args.client_secret)
    print("France Travail: requesting token ...", file=sys.stderr)
    token, ttl = get_token(cid, sec)
    print("  token ok (ttl %ds)" % ttl, file=sys.stderr)

    print("Sweeping départements %s  (%s, publiée<=%s) ..."
          % (",".join(PACA_DEPTS), args.rome or ("grandDomaine=" + GRAND_DOMAINE),
             ("%dj" % args.publiee_depuis) if args.publiee_depuis else "all active"),
          file=sys.stderr)
    raw = sweep(token, PACA_DEPTS, rome=args.rome,
                grand_domaine=None if args.rome else GRAND_DOMAINE,
                publiee_depuis=args.publiee_depuis)

    jobs, st, kept_cat, other_hosts = run(
        raw, tech_only=not args.all, keep_adjacent=args.keep_adjacent,
        drop_agencies=args.drop_agencies, drop_anon=args.drop_anon,
        links=args.links, max_per_company=max(0, args.max_per_company))

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    if args.raw_out:
        with open(args.raw_out, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    n_comp = len({j["company"] for j in jobs})
    print("\n  offers pulled          : %d" % len(raw), file=sys.stderr)
    print("  dropped anonymous      : %d" % st["anon"], file=sys.stderr)
    print("  dropped agencies/ESN   : %d" % st["agency"], file=sys.stderr)
    print("  dropped non-tech       : %d" % st["nontech"], file=sys.stderr)
    print("  dropped aggregator link: %d" % st["aggregator"], file=sys.stderr)
    if args.links == "direct":
        print("  dropped unknown link   : %d" % st["unknown_link"], file=sys.stderr)
    print("  dropped over cap (%d/co) : %d" % (args.max_per_company, st["capped"]), file=sys.stderr)
    print("  kept (%s)          : %d  from %d companies"
          % ("all" if args.all else "tech", len(jobs), n_comp), file=sys.stderr)
    if not args.all:
        print("  by category            : %s"
              % ", ".join("%s=%d" % (k, v) for k, v in kept_cat.items() if v), file=sys.stderr)
    top = {}
    for j in jobs:
        top[j["company"]] = top.get(j["company"], 0) + 1
    print("  top employers          : %s" % ", ".join(
        "%s(%d)" % (c, n) for c, n in sorted(top.items(), key=lambda x: -x[1])[:12]), file=sys.stderr)
    if other_hosts and args.links != "any":
        print("  unknown link hosts (add to KNOWN_ATS_RX / AGGREGATOR_RX): %s" % ", ".join(
            "%s(%d)" % (h, n) for h, n in sorted(other_hosts.items(), key=lambda x: -x[1])[:15]),
            file=sys.stderr)
    print("\nwrote %s" % args.output, file=sys.stderr)


if __name__ == "__main__":
    main()
