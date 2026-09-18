#!/usr/bin/env python3
"""
jobboard/linkedin_post.py — carte + légende quotidiennes pour la page
LinkedIn (Company Page) sudtechjobs, publiées via l'API Buffer.

Lancé par pipeline.sh (mode --generate, juste après render_pages.py) puis,
une fois le site déployé sur GitHub Pages (l'image doit être en ligne pour
que Buffer puisse la récupérer), par le workflow en mode --publish.

Rotation : plutôt qu'un calendrier fixe ("lundi = PM Marseille"), le script
choisit chaque jour le meilleur couple (catégorie, zone) parmi ceux qui ont
assez d'offres fraîches — "product" et "design" sont trop rares seuls
(<30 offres actives sur toute la région) pour tenir un créneau dédié, donc
fusionnés. Zones = les 4 départements PACA qui ont du volume (13/06/83/84,
CITY_DEPT de render_pages.py), + une zone PACA globale en repli pour les
catégories trop fines par département (data, product).

État (jobboard/data/linkedin_state.json, committé par la CI) :
  {"last_generated_date": "YYYY-MM-DD", "last_published_date": "YYYY-MM-DD",
   "bucket_last_used": {"eng-13": "YYYY-MM-DD", ...},
   "recent_slugs": {"<slug>": "YYYY-MM-DD", ...}}    (fenêtre RECENT_DAYS)

Secrets attendus en env (GitHub Actions), pour --publish uniquement :
  BUFFER_API_KEY, BUFFER_CHANNEL_ID   (channelId de la Company Page
                                        sudtechjobs — voir --list-channels)

Usage :
  python3 jobboard/linkedin_post.py --generate            # image + légende du jour
  python3 jobboard/linkedin_post.py --generate --dry-run  # écrit l'image/légende pour prévisu,
                                                            # mais ne touche pas la rotation/l'état
  python3 jobboard/linkedin_post.py --publish              # poste sur LinkedIn via Buffer
  python3 jobboard/linkedin_post.py --list-channels         # trouve BUFFER_CHANNEL_ID
"""
import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_pages as rp  # noqa: E402  (réutilise CITY_DEPT / PACA_DEPTS / slugify)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OFFER_INDEX_PATH = DATA / "offer_index.json"
STATE_PATH = DATA / "linkedin_state.json"
OUT_DATA_DIR = DATA / "linkedin"
OUT_SITE_DIR = ROOT / "site" / "linkedin"
FONTS_DIR = ROOT / "brand" / "fonts"
LOGO_PATH = ROOT / "brand" / "sudtechjobs-logo-transparent.png"

SITE_URL = "https://sudtechjobs.com"
MIN_OFFERS = 4       # taille minimale d'un créneau pour qu'il soit éligible
MAX_ROWS = 5          # offres affichées sur la carte / dans la légende
RECENT_DAYS = 14      # une offre déjà mise en avant n'est pas reproposée avant ça
CAP_PER_COMPANY = 2   # diversité : pas plus de 2 offres de la même boîte sur une carte

DEPT_CODES = ("13", "06", "83", "84")  # 04/05 (Alpes) : volume ~nul, exclus

# "product" et "design" sont fusionnés (design seul : ~5 offres actives sur
# toute la région, jamais assez pour un créneau à lui seul).
BUCKET_CATS = {
    "eng": ("eng",),
    "data": ("data",),
    "product": ("product", "design"),
    "tech-adjacent": ("tech-adjacent",),
}
# Cadence cible (jours entre deux passages) par catégorie, pas un poids brut :
# eng pèse ~65% de l'inventaire actif -> doit revenir souvent ; product/design
# et tech-adjacent sont plus rares -> reviennent moins souvent mais ne sont
# jamais mangés par eng grâce à ce ciblage (le score est "jours écoulés /
# cadence", donc à ancienneté égale la catégorie au cycle le plus court gagne).
CAT_TARGET_INTERVAL_DAYS = {"eng": 1.5, "data": 4.0, "product": 5.0, "tech-adjacent": 5.0}

CAT_COLOR = {"eng": "#5B9BD8", "data": "#A98AE8", "product": "#48B487", "tech-adjacent": "#C6A250"}
CAT_LABEL_SING = {"eng": "Développeur", "data": "Data / IA", "product": "Product & Design",
                   "tech-adjacent": "IT & Tech"}
CAT_LABEL_PLURAL = {"eng": "Dev & Tech", "data": "Data & IA", "product": "Product & Design",
                     "tech-adjacent": "IT & Support"}
CAT_HASHTAGS = {
    "eng": ["#DevJobs", "#TechJobs"],
    "data": ["#DataJobs", "#IA", "#DataScience"],
    "product": ["#ProductManagement", "#UXDesign"],
    "tech-adjacent": ["#ITJobs", "#Support"],
}

DAY_LABEL_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; sudtechjobs-linkedin/1.0; +https://sudtechjobs.com)",
    "Accept": "application/json",
}


# --- état -------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def prune_recent_slugs(recent_slugs, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=RECENT_DAYS)).date().isoformat()
    return {s: d for s, d in recent_slugs.items() if d >= cutoff}


# --- sélection du créneau du jour -------------------------------------------

def hashtag(city_or_label):
    s = rp.slugify(city_or_label)
    return "#" + "".join(w.capitalize() for w in s.split("-"))


def load_active_offers():
    data = json.loads(OFFER_INDEX_PATH.read_text())
    return [
        {"slug": slug, "company": v["company"], "title": v["title"], "city": v.get("city") or "",
         "category": v["category"], "first_seen": v.get("first_seen", "")}
        for slug, v in data.items()
        if not v.get("expired_at")
    ]


def build_buckets(offers):
    """-> {bucket_id: {"offers": [...], "geo_label": str, "geo_hashtags": [...], "category": str, "dept": bool}}"""
    buckets = {}
    for cat, real_cats in BUCKET_CATS.items():
        cat_offers = [o for o in offers if o["category"] in real_cats]

        for code in DEPT_CODES:
            in_dept = [o for o in cat_offers if rp.CITY_DEPT.get(rp.slugify(o["city"])) == code]
            if not in_dept:
                continue
            by_city = {}
            for o in in_dept:
                by_city[o["city"]] = by_city.get(o["city"], 0) + 1
            top_cities = [c for c, _ in sorted(by_city.items(), key=lambda kv: -kv[1])[:2]]
            buckets[f"{cat}-{code}"] = {
                "offers": in_dept, "category": cat, "dept": True,
                "geo_label": " & ".join(top_cities),
                "geo_hashtags": [hashtag(c) for c in top_cities],
            }

        # repli PACA : toujours dispo, sert de filet pour les catégories fines
        buckets[f"{cat}-paca"] = {
            "offers": cat_offers, "category": cat, "dept": False,
            "geo_label": "PACA", "geo_hashtags": ["#PACA"],
        }
    return buckets


def pick_bucket(buckets, state, today):
    recent_slugs = state.get("recent_slugs", {})
    last_used = state.get("bucket_last_used", {})
    best_id, best_score, best = None, -1.0, None

    for bid, b in buckets.items():
        total = len(b["offers"])
        if total < MIN_OFFERS:
            continue
        fresh = [o for o in b["offers"] if o["slug"] not in recent_slugs]
        if not fresh:
            continue  # rien de neuf à montrer, on saute ce créneau aujourd'hui

        last = last_used.get(bid)
        days_since = 9999 if last is None else (
            datetime.fromisoformat(today).date() - datetime.fromisoformat(last).date()).days
        score = (days_since / CAT_TARGET_INTERVAL_DAYS[b["category"]]) * (1.05 if b["dept"] else 1.0)
        if score > best_score:
            best_id, best_score, best = bid, score, b

    return best_id, best


def diversify(offers, recent_slugs):
    """Priorise les offres pas encore montrées, puis les plus récentes, avec
    au plus CAP_PER_COMPANY par entreprise pour éviter qu'une seule boîte
    truste la carte."""
    ranked = sorted(offers, key=lambda o: o.get("first_seen", ""), reverse=True)
    ranked.sort(key=lambda o: o["slug"] in recent_slugs)  # stable: pas-encore-montrées d'abord

    seen, picked, leftover = {}, [], []
    for o in ranked:
        n = seen.get(o["company"], 0)
        if n < CAP_PER_COMPANY and len(picked) < MAX_ROWS:
            picked.append(o)
            seen[o["company"]] = n + 1
        else:
            leftover.append(o)
    for o in leftover:
        if len(picked) >= MAX_ROWS:
            break
        picked.append(o)
    return picked


# --- rendu de l'image ---------------------------------------------------

def _font(name, size):
    return ImageFont.truetype(str(FONTS_DIR / name), size)


def _bricolage(size, weight=b"ExtraBold"):
    f = _font("BricolageGrotesque.ttf", size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def _truncate(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if draw.textlength(text[:mid] + ell, font=font) <= max_w:
            lo = mid + 1
        else:
            hi = mid
    return text[: max(lo - 1, 0)] + ell


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_image(offers, category, geo_label, total_count, day_label, out_path):
    W, H = 1080, 1350
    BG, CARD, LINE, INK, MUTED = "#0E1C27", "#162733", "#263B49", "#E4EDF3", "#8DA2B2"
    accent = CAT_COLOR[category]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    pad = 64

    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo.thumbnail((72, 72))
    img.paste(logo, (pad, 58), logo)
    d.text((pad + 84, 58 + 16), "sudtechjobs.com", font=_font("IBMPlexMono-SemiBold.ttf", 28), fill=INK)

    pill_font = _font("IBMPlexMono-Medium.ttf", 24)
    pill_text = day_label.upper()
    tw = d.textlength(pill_text, font=pill_font)
    px1 = W - pad
    px0 = px1 - tw - 40
    d.rounded_rectangle([px0, 58, px1, 104], radius=23, outline=accent, width=2)
    d.text(((px0 + px1) / 2, 81), pill_text, font=pill_font, fill=accent, anchor="mm")

    y = 190
    d.text((pad, y), f"{total_count} offres", font=_bricolage(78), fill=INK)
    y += 92
    d.text((pad, y), CAT_LABEL_SING[category], font=_bricolage(78), fill=accent)
    y += 92
    for ln in _wrap(d, f"à {geo_label}", _bricolage(52, b"SemiBold"), W - 2 * pad):
        d.text((pad, y), ln, font=_bricolage(52, b"SemiBold"), fill=MUTED)
        y += 60

    y += 28
    d.line([(pad, y), (W - pad, y)], fill=LINE, width=2)
    y += 40

    row_company = _font("IBMPlexMono-SemiBold.ttf", 30)
    row_title = _font("IBMPlexMono-Regular.ttf", 26)
    row_city = _font("IBMPlexMono-Regular.ttf", 22)
    for off in offers[:MAX_ROWS]:
        d.ellipse([pad, y + 10, pad + 12, y + 22], fill=accent)
        tx = pad + 32
        d.text((tx, y), off["company"], font=row_company, fill=INK)
        y += 38
        d.text((tx, y), _truncate(d, off["title"], row_title, W - tx - pad), font=row_title, fill=MUTED)
        y += 30
        if off.get("city"):
            d.text((tx, y), off["city"], font=row_city, fill=accent)
            y += 34
        y += 18

    cta_h = 150
    d.rectangle([0, H - cta_h, W, H], fill=CARD)
    d.text((pad, H - cta_h + 34), "Toutes les offres tech PACA →",
            font=_font("IBMPlexMono-SemiBold.ttf", 30), fill=INK)
    d.text((pad, H - cta_h + 78), "sudtechjobs.com",
            font=_font("IBMPlexMono-SemiBold.ttf", 32), fill=accent)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def build_caption(offers, category, geo_label, geo_hashtags):
    role = CAT_LABEL_PLURAL[category]
    lines = [f"🚀 Nouvelles offres {role} en {geo_label} 🚀", ""]
    for o in offers:
        lines.append(f"▶ {o['title']} — {o['company']}")
        lines.append(f"🔗 {SITE_URL}/offre/{o['slug']}.html")
        lines.append("")
    lines.append(f"Toutes les offres tech PACA, mises à jour chaque jour 👉 {SITE_URL}")
    lines.append("")
    tags = ["#SudTechJobs", "#TechPACA"] + geo_hashtags + CAT_HASHTAGS[category] + ["#Hiring", "#Recrutement"]
    lines.append(" ".join(tags))
    return "\n".join(lines)


# --- Buffer (GraphQL) --------------------------------------------------------

def buffer_graphql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.buffer.com", data=body, method="POST",
        headers={**API_HEADERS, "Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def cmd_list_channels(args):
    token = os.environ.get("BUFFER_API_KEY", "")
    if not token:
        print("BUFFER_API_KEY manquant dans l'environnement.", file=sys.stderr)
        return 1
    try:
        orgs = buffer_graphql("query { account { organizations { id name } } }", {}, token)
    except urllib.error.HTTPError as e:
        print(f"Buffer a refusé la requête ({e.code} {e.reason}): {e.read().decode(errors='replace')[:400]}",
              file=sys.stderr)
        print("-> vérifie que BUFFER_API_KEY contient bien ta vraie clé (pas un texte d'exemple).",
              file=sys.stderr)
        return 1
    print(json.dumps(orgs, indent=2, ensure_ascii=False))
    org_id = (((orgs.get("data") or {}).get("account") or {}).get("organizations") or [{}])[0].get("id")
    if not org_id:
        return 1
    # organizationId inlined (not passed as a $variable) to sidestep guessing
    # Buffer's exact GraphQL scalar type name for it; org_id comes from our
    # own prior response, not user input, so no injection concern here.
    try:
        channels = buffer_graphql(
            'query { channels(input:{organizationId:"%s"}) '
            "{ id name service type displayName } }" % org_id,
            {}, token,
        )
    except urllib.error.HTTPError as e:
        print(f"Buffer a refusé la requête channels ({e.code} {e.reason}): "
              f"{e.read().decode(errors='replace')[:400]}", file=sys.stderr)
        return 1
    print(json.dumps(channels, indent=2, ensure_ascii=False))
    return 0


def cmd_publish(args):
    date = datetime.now(timezone.utc).date().isoformat()
    meta_path = OUT_DATA_DIR / f"{date}.json"
    if not meta_path.exists():
        print(f"linkedin_post: pas de {meta_path.name} — rien à publier aujourd'hui.")
        return 0

    state = load_state()
    if not args.force and state.get("last_published_date") == date:
        print(f"linkedin_post: déjà publié aujourd'hui ({date}) — on saute.")
        return 0

    token = os.environ.get("BUFFER_API_KEY", "")
    channel_id = os.environ.get("BUFFER_CHANNEL_ID", "")
    if not (token and channel_id):
        print("linkedin_post: BUFFER_API_KEY / BUFFER_CHANNEL_ID manquants — abandon.", file=sys.stderr)
        return 0

    meta = json.loads(meta_path.read_text())
    # caption_path est stocké relatif à ROOT (jobboard/), pas au cwd du process
    # — le step "Publish" tourne depuis la racine du repo, d'où le join ici.
    caption = (ROOT / meta["caption_path"]).read_text()

    # Input inlined as a GraphQL literal (rather than a $variable) since
    # Buffer's exact input-object type name isn't confirmed — this mirrors
    # the literal shape shown in Buffer's own "create image post" example.
    # json.dumps() doubles as a GraphQL string-literal escaper (same rules).
    mutation = """
    mutation CreatePost {
      createPost(input: {
        text: %s
        channelId: %s
        schedulingType: automatic
        mode: shareNow
        assets: [{ image: { url: %s } }]
      }) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }""" % (json.dumps(caption), json.dumps(channel_id), json.dumps(meta["image_url"]))
    try:
        result = buffer_graphql(mutation, {}, token)
    except urllib.error.HTTPError as e:
        print(f"linkedin_post: échec Buffer ({e.code} {e.reason}): {e.read()[:400]}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"linkedin_post: échec Buffer: {e}", file=sys.stderr)
        return 0

    payload = ((result.get("data") or {}).get("createPost") or {})
    if payload.get("message"):
        print(f"linkedin_post: Buffer a refusé le post: {payload['message']}", file=sys.stderr)
        return 0

    print(f"linkedin_post: publié sur LinkedIn (bucket {meta['bucket_id']}).")
    state["last_published_date"] = date
    save_state(state)
    return 0


def cmd_generate(args):
    date = datetime.now(timezone.utc).date().isoformat()
    state = load_state()
    if not args.dry_run and not args.force and state.get("last_generated_date") == date:
        print(f"linkedin_post: déjà généré aujourd'hui ({date}) — on saute.")
        return 0

    offers = load_active_offers()
    buckets = build_buckets(offers)
    bucket_id, bucket = pick_bucket(buckets, state, date)
    if bucket is None:
        print("linkedin_post: aucun créneau n'a assez d'offres fraîches aujourd'hui — on saute.")
        return 0

    recent_slugs = state.get("recent_slugs", {})
    picked = diversify(bucket["offers"], recent_slugs)
    day_label = DAY_LABEL_FR[datetime.fromisoformat(date).weekday()]

    image_path = OUT_SITE_DIR / f"{date}.png"
    render_image(picked, bucket["category"], bucket["geo_label"], len(bucket["offers"]), day_label, image_path)
    caption = build_caption(picked, bucket["category"], bucket["geo_label"], bucket["geo_hashtags"])
    caption_path = OUT_DATA_DIR / f"{date}.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text(caption)

    meta = {
        "date": date, "bucket_id": bucket_id, "category": bucket["category"],
        "geo_label": bucket["geo_label"], "offer_count": len(bucket["offers"]),
        "image_path": str(image_path.relative_to(ROOT)),
        "image_url": f"{SITE_URL}/linkedin/{date}.png",
        "caption_path": str(caption_path.relative_to(ROOT)),
    }
    (OUT_DATA_DIR / f"{date}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    print(f"linkedin_post: généré — {bucket_id} ({len(picked)}/{len(bucket['offers'])} offres) -> {image_path}")

    if args.dry_run:
        print(caption)
        return 0

    state["last_generated_date"] = date
    state.setdefault("bucket_last_used", {})[bucket_id] = date
    recent = state.setdefault("recent_slugs", {})
    for o in picked:
        recent[o["slug"]] = date
    state["recent_slugs"] = prune_recent_slugs(recent, date)
    save_state(state)
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--generate", action="store_true", help="choisit le créneau du jour, rend la carte + la légende")
    g.add_argument("--publish", action="store_true", help="poste sur la Company Page LinkedIn via Buffer")
    g.add_argument("--list-channels", action="store_true", help="liste les channels Buffer (trouver BUFFER_CHANNEL_ID)")
    ap.add_argument("--dry-run", action="store_true", help="--generate : n'écrit pas l'état")
    ap.add_argument("--force", action="store_true", help="ignore le garde-fou une-fois-par-jour")
    args = ap.parse_args()

    if args.list_channels:
        return cmd_list_channels(args)
    if args.generate:
        return cmd_generate(args)
    return cmd_publish(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
