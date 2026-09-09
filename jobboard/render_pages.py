#!/usr/bin/env python3
"""Layer 5 — static SEO pages.

Reads jobboard/site/jobs.json (built by build.py) and writes, into jobboard/site/:

    offre/<slug>.html     one page per job — JobPosting JSON-LD, OpenGraph, canonical
                          (or a noindex tombstone once the offer leaves the feed)
    emploi/<facet>.html   filtered list pages (métier × ville, techno × ville, télétravail…)
    emploi/index.html     hub linking every facet page
    sitemap.xml           sitemap index -> sitemap-pages.xml + sitemap-offres.xml
    robots.txt            points crawlers at the sitemap index

A closed posting is de-listed the Google-for-Jobs way: dropped from the offers
sitemap, JobPosting markup removed, `noindex` + a redirect to its métier facet,
kept TOMBSTONE_DAYS then left to 404. State lives in data/offer_index.json
(committed by CI, like data/seen.json).

All the HTML/XML is build output: git-ignored, regenerated on every run, deployed
from the GitHub Pages artifact (not from git). The SPA home (index.html) is untouched.

    python3 jobboard/render_pages.py
    SITE_URL=https://exemple.fr python3 jobboard/render_pages.py   # override the base URL
"""
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
DATA = os.path.join(HERE, "data")
FEED = os.path.join(SITE, "jobs.json")

# slug -> {first_seen,last_seen,expired_at,title,company,city,category,facet_slug}
# persisted (committed by CI, like data/seen.json) so a job that drops out of the
# feed leaves a noindex tombstone instead of a bare 404 — Google for Jobs wants
# closed postings de-listed, not 404-storming its crawler.
OFFER_INDEX = os.path.join(DATA, "offer_index.json")
TOMBSTONE_DAYS = 120   # keep the tombstone this long after an offer vanishes, then let it 404

# one row per hiring company (build_companies.py); optional — company pages are
# skipped with a warning if it's missing.
COMPANIES = os.path.join(SITE, "companies.json")

SITE_URL = os.environ.get(
    "SITE_URL", "https://sudtechjobs.com"
).rstrip("/")

# destination host -> label for the "Postuler sur …" button. Keyed on a
# substring of the apply URL's host; first match wins.
APPLY_HOSTS = (
    ("welcometothejungle.com", "Welcome to the Jungle"),
    ("smartrecruiters.com", "SmartRecruiters"),
    ("lever.co", "Lever"),
    ("ashbyhq.com", "Ashby"),
    ("teamtailor.com", "Teamtailor"),
    ("recruitee.com", "Recruitee"),
    ("taleez.com", "Taleez"),
    ("greenhouse.io", "Greenhouse"),
    ("workable.com", "Workable"),
    ("francetravail.fr", "France Travail"),
    ("pole-emploi.fr", "France Travail"),
)


def apply_dest_label(url):
    """Human label for the apply destination, or "" when we can't name it
    (plain "Postuler" then)."""
    if not url:
        return ""
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower()
    for frag, label in APPLY_HOSTS:
        if frag in host:
            return label
    return ""

MIN_FACET = 3        # min jobs for a ville / métier×ville / techno×ville page
MIN_STACK = 8        # min jobs for a techno-only page
VALID_DAYS = 90      # JobPosting.validThrough = datePosted + this

# tags that show up in `stack` but aren't a tech skill worth its own landing page
STACK_DENY = {"claude", "excel", "notion", "slack", "google-ads", "google-analytics",
              "confluence", "jira", "office", "microsoft-office", "powerpoint", "word",
              "sonar", "windows"}

CATS = {
    "eng":           ("Développeur", "Développeur · Infra"),
    "data":          ("Data / IA", "Data · IA"),
    "product":       ("Product Manager", "Product"),
    "design":        ("Design", "Design · UX"),
    "tech-adjacent": ("IT & Tech", "IT & support"),
}

# PACA département code -> (slug, name, "dans <article+name>" for the H1).
PACA_DEPTS = {
    "04": ("alpes-de-haute-provence", "Alpes-de-Haute-Provence", "dans les Alpes-de-Haute-Provence"),
    "05": ("hautes-alpes", "Hautes-Alpes", "dans les Hautes-Alpes"),
    "06": ("alpes-maritimes", "Alpes-Maritimes", "dans les Alpes-Maritimes"),
    "13": ("bouches-du-rhone", "Bouches-du-Rhône", "dans les Bouches-du-Rhône"),
    "83": ("var", "Var", "dans le Var"),
    "84": ("vaucluse", "Vaucluse", "dans le Vaucluse"),
}

# slugify(city) -> département code. Cities missing here simply don't roll up
# into a département page (harmless); render_pages prints the misses so the map
# can be extended. Covers what the feed carries today plus other likely PACA
# towns so a feed refresh doesn't silently drop offers from the dept pages.
CITY_DEPT = {c: "13" for c in (
    "aix-en-provence", "ais-en-provence", "marseille",
    "marseille-2e-arrondissement", "la-ciotat", "la-ciotat-la-vigie",
    "vitrolles", "marignane", "aubagne", "gemenos", "rousset", "berre-l-etang",
    "la-penne-sur-huveaune", "penne-sur-huveaune", "la-fare-les-oliviers",
    "saint-paul-les-durance", "meyreuil", "port-de-bouc", "fos-sur-mer",
    "martigues", "salon-de-provence", "istres", "gardanne", "bouc-bel-air",
    "cabries", "les-pennes-mirabeau", "chateauneuf-les-martigues", "peynier",
)}
CITY_DEPT.update({c: "06" for c in (
    "sophia-antipolis", "valbonne", "nice", "biot", "cannes", "mougins",
    "cagnes-sur-mer", "carros", "la-trinite", "antibes", "grasse", "vallauris",
    "le-cannet", "menton", "villeneuve-loubet", "saint-laurent-du-var",
)})
CITY_DEPT.update({c: "83" for c in (
    "ollioules", "toulon", "six-fours-les-plages", "saint-tropez",
    "la-valette-du-var", "valette-du-var", "frejus", "la-seyne-sur-mer",
    "draguignan", "la-garde", "hyeres", "sanary-sur-mer", "cuers", "brignoles",
    "saint-raphael", "le-pradet",
)})
CITY_DEPT.update({c: "84" for c in (
    "avignon", "orange", "carpentras", "cavaillon", "sorgues", "le-pontet",
    "l-isle-sur-la-sorgue", "pertuis", "apt",
)})

# schema.org employmentType, matched on a cleaned contract string
EMP_TYPE = [
    ("cdi", "FULL_TIME"), ("permanent", "FULL_TIME"), ("fulltime", "FULL_TIME"),
    ("full time", "FULL_TIME"), ("full-time", "FULL_TIME"),
    ("cdd", "TEMPORARY"), ("temporary", "TEMPORARY"), ("interim", "TEMPORARY"),
    ("stage", "INTERN"), ("intern", "INTERN"), ("alternance", "INTERN"),
    ("apprenti", "INTERN"),
    ("freelance", "CONTRACTOR"), ("contractor", "CONTRACTOR"),
    ("part", "PART_TIME"), ("vie", "OTHER"), ("vie ", "OTHER"),
]

REMOTE_FULL = {"remote", "full remote"}


def esc(s):
    return html.escape(str(s or ""), quote=True)


def slugify(s):
    s = str(s or "").lower()
    for a, b in (("c++", "cpp"), ("c#", "csharp"), (".net", "dotnet"), ("f#", "fsharp"),
                 ("node.js", "nodejs"), ("next.js", "nextjs")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)


def text_to_html(txt):
    txt = (txt or "").replace("\r\n", "\n")
    out = []
    for para in re.split(r"\n\s*\n", txt.strip()):
        para = re.sub(r"\s*\n\s*", " ", para.strip())
        if para:
            out.append("<p>" + esc(para) + "</p>")
    return "\n".join(out)


def emp_type(contract):
    c = unicodedata.normalize("NFKD", str(contract or "")).encode(
        "ascii", "ignore").decode().lower()
    for needle, val in EMP_TYPE:
        if needle in c:
            return val
    return None


SAL_RX = re.compile(
    r"(\d[\d\s]*\d|\d)\s*(k)?\s*(?:[-–/]|à|to)\s*(\d[\d\s]*\d|\d)\s*(k)?", re.I)
SAL_ONE_RX = re.compile(r"(\d[\d\s]*\d|\d)\s*(k)?", re.I)


def parse_salary(s):
    if not s:
        return None
    txt = str(s)
    unit = "YEAR"
    if re.search(r"/\s*(j|jour|day)", txt, re.I):
        unit = "DAY"
    elif re.search(r"/\s*(m|mois|month)", txt, re.I):
        unit = "MONTH"
    m = SAL_RX.search(txt)

    def val(num, k):
        n = int(re.sub(r"\s", "", num))
        return n * 1000 if (k or (n < 1000 and unit == "YEAR")) else n

    if m:
        lo, hi = val(m.group(1), m.group(2)), val(m.group(3), m.group(4))
        if lo and hi:
            return {"minValue": lo, "maxValue": hi, "unitText": unit}
    m = SAL_ONE_RX.search(txt)
    if m:
        v = val(m.group(1), m.group(2))
        if v:
            return {"value": v, "unitText": unit}
    return None


def job_slug(j):
    base = slugify("%s-%s" % (j.get("company", ""), j.get("title", "")))[:70].strip("-")
    h = hashlib.sha1(j["id"].encode()).hexdigest()[:6]
    return "%s-%s" % (base, h) if base else h


# --------------------------------------------------------------------------- #
#  HTML shell                                                                 #
# --------------------------------------------------------------------------- #
CSS = """
:root{
  --bg:#EDF2F6; --card:#FFFFFF; --card-2:#F4F8FB;
  --ink:#16303F; --muted:#5F7488; --line:#DCE4EC;
  --accent:#F2A63B; --accent-ink:#93600F; --on-accent:#3A2708;
  --brand:#7CBDE8; --brand-ink:#2C6C9E;
  --wash:#FBEBD6; --pine:#2C9A6B;
  --shadow:0 1px 2px rgba(22,48,63,.05), 0 12px 28px -16px rgba(22,48,63,.18);
  --iris:
    radial-gradient(105% 70% at 96% -8%,  color-mix(in srgb,var(--accent) 22%,transparent) 0%, transparent 55%),
    radial-gradient(105% 80% at -8% -4%,  color-mix(in srgb,var(--brand) 46%,transparent) 0%, transparent 52%),
    radial-gradient(120% 55% at 50% 118%, color-mix(in srgb,var(--brand) 16%,transparent) 0%, transparent 60%);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0E1C27; --card:#162733; --card-2:#1C303D;
    --ink:#E4EDF3; --muted:#8DA2B2; --line:#263B49;
    --accent:#E7A24A; --accent-ink:#F1C089; --on-accent:#2A1B06;
    --brand:#7CBDE8; --brand-ink:#8CC6ED;
    --wash:#2A2013; --pine:#48B487;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 14px 32px -18px rgba(0,0,0,.6);
  }
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;color:var(--ink);min-height:100vh;
 font:15px/1.6 "Hanken Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 -webkit-font-smoothing:antialiased;background:var(--iris),var(--bg);background-attachment:fixed}
a{color:var(--brand-ink);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2.5px solid var(--accent);outline-offset:2px;border-radius:6px}
.wrap{max-width:820px;margin:0 auto;padding:0 20px 80px}
header{padding:24px 0 6px}
.brandline{display:flex;align-items:center;gap:12px}
.brandline svg{flex:none;width:40px;height:40px;filter:drop-shadow(0 4px 10px rgba(22,48,63,.16))}
.brandline .wm{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:22px;
 letter-spacing:-.02em;color:var(--ink);line-height:1}
.brandline .wm i{color:var(--accent);font-style:normal}
.brandline .rb{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:500;
 color:var(--accent-ink);background:color-mix(in srgb,var(--accent) 20%,transparent);
 border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);border-radius:999px;
 padding:5px 10px;letter-spacing:.02em;white-space:nowrap}
a.brandline:hover{text-decoration:none}a.brandline:hover .wm{color:var(--brand-ink)}
nav.bc{font-size:12.5px;color:var(--muted);margin:16px 0 4px}
nav.bc a{color:var(--muted)}
h1{font-family:"Bricolage Grotesque",sans-serif;font-weight:600;font-size:23px;line-height:1.3;
 letter-spacing:-.01em;margin:12px 0 6px;text-wrap:balance}
.sub{color:var(--muted);font-size:14px;margin:0 0 18px}
.sub a{color:var(--brand-ink);text-decoration:underline;text-underline-offset:2px;
 text-decoration-color:color-mix(in srgb,var(--brand-ink) 38%,transparent)}
.sub a:hover{text-decoration-color:currentColor}
.facets{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 22px}
.facets a{font-size:12.5px;background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:5px 10px;color:var(--brand-ink);box-shadow:var(--shadow)}
.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:15px 17px;
 margin:0 0 20px;box-shadow:var(--shadow)}
.k{display:flex;flex-wrap:wrap;gap:7px;font-size:12px;color:var(--muted);margin:8px 0 0}
.k span{background:var(--card-2);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.k .sal{color:var(--pine);border-color:color-mix(in srgb,var(--pine) 35%,transparent);
 background:color-mix(in srgb,var(--pine) 12%,transparent);font-weight:500}
.stack{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 0}
.stack b{font:500 11.5px/1 "IBM Plex Mono",ui-monospace,monospace;background:var(--card-2);
 color:var(--brand-ink);border:1px solid var(--line);border-radius:6px;padding:4px 7px}
.apply{display:inline-block;margin:16px 0 4px;font-family:"IBM Plex Mono",monospace;font-weight:600;
 font-size:13px;background:var(--accent);color:var(--on-accent);border-radius:10px;padding:11px 18px;
 box-shadow:0 8px 22px -8px color-mix(in srgb,var(--accent) 75%,transparent)}
.apply:hover{text-decoration:none;filter:brightness(1.05)}
.desc{margin:18px 0 0}.desc p{margin:0 0 11px}
.about{margin:6px 0 0}.about .sub{font-size:12.5px;margin:0 0 8px}
.about a{color:var(--brand-ink)}
h2{font-family:"Bricolage Grotesque",sans-serif;font-size:15px;margin:26px 0 8px}
ul.jobs{list-style:none;margin:0;padding:0}
ul.jobs li{background:var(--card);border:1px solid var(--line);border-radius:12px;
 margin:0 0 11px;box-shadow:var(--shadow);overflow:hidden}
ul.jobs li a{display:block;padding:13px 15px;font-family:"Bricolage Grotesque",sans-serif;
 font-weight:600;color:var(--ink);font-size:15px}
ul.jobs li a:hover{text-decoration:none;background:var(--card-2)}
ul.jobs .co{display:block;color:var(--muted);font-size:13px;font-weight:400;margin:3px 0 0}
footer{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
/* company page */
.cover{height:150px;border-radius:14px;background:var(--card-2) center/cover no-repeat;
 border:1px solid var(--line);margin:8px 0 12px}
.cohead{display:flex;gap:15px;align-items:flex-end;padding:0 4px}
.cohead .lg{width:74px;height:74px;border-radius:16px;background:#fff;border:1px solid var(--line);
 object-fit:contain;padding:7px;box-shadow:var(--shadow);flex:none}
.cover + .cohead .lg{margin-top:-56px}
.cohead .lg.ph{display:flex;align-items:center;justify-content:center;font-family:"Bricolage Grotesque",sans-serif;
 font-weight:700;font-size:30px;color:var(--brand-ink);background:var(--card)}
.cohead h1{margin:0 0 3px}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 20px}
.badges span{font-size:11.5px;background:color-mix(in srgb,var(--brand) 16%,transparent);color:var(--brand-ink);
 border:1px solid color-mix(in srgb,var(--brand) 34%,transparent);border-radius:999px;padding:3px 9px}
.badges span.eco{background:color-mix(in srgb,var(--pine) 14%,transparent);color:var(--pine);
 border-color:color-mix(in srgb,var(--pine) 34%,transparent)}
dl.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px 18px;margin:0}
dl.facts div{margin:0}
dl.facts dt{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
dl.facts dd{margin:2px 0 0;font-size:14px;font-weight:500}
.social{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0 0}
.social a{font-size:12px;background:var(--card-2);border:1px solid var(--line);border-radius:8px;padding:5px 10px}
.spark{display:flex;align-items:flex-end;gap:3px;height:44px;margin:8px 0 4px}
.spark i{flex:1;background:var(--brand);border-radius:2px 2px 0 0;min-height:2px;opacity:.75}
.spark i.now{background:var(--accent);opacity:1}
.mini{display:flex;flex-wrap:wrap;gap:7px;margin:6px 0 0}
.mini a,.mini span{font-size:12.5px;background:var(--card-2);border:1px solid var(--line);border-radius:8px;
 padding:4px 9px;color:var(--brand-ink)}
/* audience-notice bar (Umami is cookieless — informational, not a consent gate) */
#cookie-notice{position:fixed;left:12px;right:12px;bottom:12px;max-width:560px;margin:0 auto;
 background:var(--card);border:1px solid var(--line);border-radius:12px;
 box-shadow:0 12px 34px -12px rgba(22,48,63,.32);padding:11px 14px;display:flex;gap:12px;
 align-items:center;font-size:12.5px;color:var(--muted);z-index:60}
#cookie-notice p{margin:0}
#cookie-notice a{color:var(--brand-ink)}
#cookie-notice button{flex:none;font:inherit;font-weight:600;border:1px solid var(--line);
 background:var(--card-2);color:var(--ink);border-radius:8px;padding:6px 13px;cursor:pointer}
#cookie-notice button:hover{border-color:var(--brand)}
/* legal pages */
.legal{max-width:680px}
.legal h2{font-family:"Bricolage Grotesque",sans-serif;font-size:16px;margin:28px 0 8px}
.legal p,.legal li{font-size:14px;line-height:1.65;color:var(--ink)}
.legal ul{margin:6px 0 13px;padding-left:20px}
.legal li{margin:0 0 5px}
.legal a{color:var(--brand-ink);text-decoration:underline}
.legal code{font:500 12.5px/1 "IBM Plex Mono",ui-monospace,monospace;background:var(--card-2);
 border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.legal .upd{color:var(--muted);font-size:12.5px;margin:0 0 4px}
.legal .note{background:var(--card-2);border:1px solid var(--line);border-radius:10px;
 padding:12px 14px;font-size:13px;color:var(--muted);margin:16px 0}
"""


def shell(*, title, description, canonical, head_extra="", body):
    return """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canon}">
<meta property="og:type" content="website">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{canon}">
<meta name="twitter:card" content="summary">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="96x96" href="/favicon-96.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=Hanken+Grotesk:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<script defer src="https://cloud.umami.is/script.js" data-website-id="b11bf5e9-a867-4c49-8dc5-b1a7b4cc6eb6"></script>
<style>{css}</style>
{head_extra}
</head>
<body>
<div class="wrap">
<header>
  <a class="brandline" href="{home}">
    <svg viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <rect x="5" y="8" width="42" height="34" rx="8" fill="var(--brand)" stroke="var(--ink)" stroke-width="3.5"/>
      <path d="M22 42v6M14 50h16" stroke="var(--ink)" stroke-width="3.5" stroke-linecap="round"/>
      <circle cx="20" cy="24" r="2.6" fill="var(--ink)"/><circle cx="32" cy="24" r="2.6" fill="var(--ink)"/>
      <path d="M20 31c2.4 2.6 7.6 2.6 10 0" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>
      <circle cx="44" cy="40" r="12" fill="var(--wash)" stroke="var(--accent)" stroke-width="4.5"/>
      <path d="M53 49l7 7" stroke="var(--accent)" stroke-width="5" stroke-linecap="round"/>
    </svg>
    <span class="wm">sudtechjobs<i>.</i></span>
    <span class="rb">RÉGION · PACA</span>
  </a>
</header>
{body}
<footer>
  <a href="{home}">Toutes les offres</a> ·
  <a href="{hub}">Parcourir par ville &amp; techno</a> ·
  <a href="{companies}">Entreprises</a> ·
  job board tech du sud de la France
  <br><br>Une offre à ajouter, une remarque, ou juste envie de papoter du Sud&nbsp;?
  Écrivez-moi, ça fait toujours plaisir 🫰
  <a href="mailto:hello@sudtechjobs.com">✉️ hello@sudtechjobs.com</a>
  <br><br><a href="/mentions-legales.html">Mentions légales</a> ·
  <a href="/cgu.html">CGU</a> ·
  <a href="/confidentialite.html">Confidentialité</a>
</footer>
</div>
<div id="cookie-notice" hidden>
  <p>Mesure d’audience sans cookie ni donnée personnelle (Umami).
  <a href="/confidentialite.html">En savoir plus</a>.</p>
  <button type="button" id="cookie-ok">OK</button>
</div>
<script>
(function(){{try{{
  var k="sjt-notice-ok";if(localStorage.getItem(k))return;
  var n=document.getElementById("cookie-notice");if(!n)return;n.hidden=false;
  document.getElementById("cookie-ok").addEventListener("click",function(){{
    n.hidden=true;try{{localStorage.setItem(k,"1")}}catch(e){{}}
  }});
}}catch(e){{}}}})();
</script>
</body>
</html>""".format(
        title=esc(title), desc=esc(description), canon=esc(canonical),
        css=CSS, head_extra=head_extra, body=body,
        home=SITE_URL + "/", hub=SITE_URL + "/emploi/",
        companies=SITE_URL + "/entreprise/",
    )


def jsonld(obj):
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False) + "</script>")


# --------------------------------------------------------------------------- #
#  offer pages                                                                #
# --------------------------------------------------------------------------- #
def render_offer(j, similar, same_company=None):
    slug = j["_slug"]
    canonical = "%s/offre/%s.html" % (SITE_URL, slug)
    city = j.get("city") or ""
    is_remote = city == "Remote" or (j.get("remote") or "") in REMOTE_FULL
    posted = j.get("published_at") or j.get("first_seen") or ""
    # apply button: there is always a destination — `apply_url` (the real ATS
    # deep link, when the WTTJ enrichment resolved one) or, failing that, the
    # `url` (the WTTJ posting, which has its own "Postuler" flow). The label is
    # derived from the destination host, not the noisy `ats` field.
    apply_href = j.get("apply_url") or j.get("url") or ""
    dest_label = apply_dest_label(apply_href)
    apply_btn = (
        '<a class="apply" href="%s" target="_blank" rel="nofollow noopener">'
        "Postuler%s</a>" % (
            esc(apply_href),
            " sur " + esc(dest_label) if dest_label else "")
    ) if apply_href else ""

    posted_h = ""
    try:
        posted_h = datetime.fromisoformat(
            posted.replace("Z", "+00:00")).strftime("%d/%m/%Y") if posted else ""
    except ValueError:
        posted_h = ""
    bits = [b for b in [
        None if is_remote else city,
        "télétravail" if is_remote else (j.get("remote_detail") or None),
        j.get("contract"),
        j.get("experience"),
        ("publiée le " + posted_h) if posted_h else None,
    ] if b]
    krow = "".join('<span>%s</span>' % esc(b) for b in bits)
    if j.get("salary"):
        krow += '<span class="sal">%s</span>' % esc(j["salary"])

    stack = j.get("stack") or []
    stack_html = ""
    if stack:
        stack_html = '<div class="stack">%s</div>' % "".join(
            "<b>%s</b>" % esc(s) for s in stack[:16])

    body_txt = j.get("description") or j.get("description_excerpt") or ""
    desc_html = text_to_html(body_txt) or (
        "<p>Offre publiée par %s%s.%s</p>" % (
            esc(j.get("company")),
            " à " + esc(city) if city and not is_remote else "",
            " Consultez l'annonce complète et postulez via le bouton ci-dessus."
            if apply_btn else ""))
    profile_html = ""
    if j.get("profile_excerpt"):
        profile_html = "<h2>Profil recherché</h2>\n" + text_to_html(j["profile_excerpt"])
    benefits_html = ""
    if j.get("benefits_preview"):
        benefits_html = "<h2>Avantages</h2>\n<ul>%s</ul>" % "".join(
            "<li>%s</li>" % esc(b) for b in j["benefits_preview"])

    # ---- "À propos de {company}" — a short company blurb + key facts, reusing
    # the WTTJ profile we already carry for the company page (≈75% of companies).
    about_html = ""
    crec = j.get("_company") or {}
    cprof = crec.get("profile") or {}
    about_txt = re.sub(r"\s+", " ", (cprof.get("description") or "")).strip()
    if about_txt:
        if len(about_txt) > 340:
            about_txt = about_txt[:340].rsplit(" ", 1)[0].rstrip(".,;:") + " […]"
        cfacts = " · ".join(x for x in [
            (cprof.get("sectors") or [None])[0],
            _fmt_headcount(cprof.get("headcount")) if cprof.get("headcount") else None,
            ("créée en %s" % cprof["founded"]) if cprof.get("founded") else None,
            ("siège à %s" % cprof["hq_city"]) if cprof.get("hq_city") else None,
        ] if x)
        more = ('<a href="../entreprise/%s.html">→ Fiche complète de %s : '
                "toutes ses offres, sa stack, ses chiffres</a>"
                % (crec["slug"], esc(crec["name"]))) if crec.get("slug") else ""
        about_html = (
            '<h2>À propos de %s</h2>\n<div class="desc about"><p>%s</p>%s%s</div>' % (
                esc(crec.get("name") or j.get("company") or ""),
                esc(about_txt),
                ('<p class="sub">%s</p>' % esc(cfacts)) if cfacts else "",
                ("<p>%s</p>" % more) if more else ""))

    cat = j.get("category")
    cat_label = CATS.get(cat, (cat, cat))[0]
    facet_link = ""
    fslug = j.get("_facet_slug")
    if fslug:
        has_city = fslug != slugify(cat or "")
        where = (" à " + esc(city)) if (has_city and not is_remote and city) else " en PACA"
        facet_link = '<p><a href="../emploi/%s.html">→ Toutes les offres %s%s</a></p>' % (
            fslug, esc(cat_label), where)

    def _job_list(items):
        return "<ul class=\"jobs\">%s</ul>" % "".join(
            '<li><a href="%s.html"><span class="t">%s</span>'
            '<span class="co">%s%s</span></a></li>' % (
                s["_slug"], esc(s["title"]), esc(s.get("company")),
                " · " + esc(s.get("city")) if s.get("city") else "")
            for s in items)

    sim_html = ""
    if similar:
        sim_html = "<h2>Offres similaires</h2>\n" + _job_list(similar)
    cslug = j.get("_company_slug")
    co_link = ('<a href="../entreprise/%s.html">%s</a>' % (cslug, esc(j.get("company")))) \
        if cslug else esc(j.get("company"))
    if same_company:
        sim_html += "\n<h2>Autres offres chez %s</h2>\n%s" % (co_link, _job_list(same_company))
    if cslug:
        sim_html += ('\n<p><a href="../entreprise/%s.html">→ Fiche entreprise %s : '
                     "toutes ses offres, sa stack, ses chiffres</a></p>"
                     % (cslug, esc(j.get("company"))))

    meta_desc = (j.get("description_excerpt") or body_txt or
                 "%s chez %s" % (j.get("title"), j.get("company")))
    meta_desc = re.sub(r"\s+", " ", meta_desc).strip()[:180]

    # ---- JobPosting JSON-LD -------------------------------------------------
    org = {"@type": "Organization", "name": j.get("company") or "—"}
    if j.get("careers_url"):
        org["sameAs"] = j["careers_url"]
    if j.get("logo"):
        org["logo"] = j["logo"]

    ld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": j.get("title") or "",
        "description": desc_html,
        "hiringOrganization": org,
        "url": canonical,
        "directApply": False,
        # Google for Jobs: `identifier` is recommended and helps it dedupe the
        # same posting seen through several aggregators.
        "identifier": {
            "@type": "PropertyValue",
            "name": j.get("company") or "sudtechjobs",
            "value": j.get("id") or slug,
        },
    }
    # `datePosted` is REQUIRED — never emit a JobPosting without it.
    dp = (posted or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    ld["datePosted"] = dp
    try:
        d0 = datetime.fromisoformat((posted or dp).replace("Z", "+00:00"))
        ld["validThrough"] = (d0 + timedelta(days=VALID_DAYS)).date().isoformat()
    except ValueError:
        pass
    et = emp_type(j.get("contract"))
    if et:
        ld["employmentType"] = et
    if is_remote:
        ld["jobLocationType"] = "TELECOMMUTE"
        ld["applicantLocationRequirements"] = {"@type": "Country", "name": "France"}
    if city and city != "Remote":
        ld["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressRegion": "Provence-Alpes-Côte d'Azur",
                "addressCountry": "FR",
            },
        }
    elif not is_remote:
        # REQUIRED unless the role is TELECOMMUTE: fall back to a region-level
        # place so the posting still validates when we have no precise city.
        ld["jobLocation"] = {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressRegion": "Provence-Alpes-Côte d'Azur",
                "addressCountry": "FR",
            },
        }
    sal = parse_salary(j.get("salary"))
    if sal:
        qv = {"@type": "QuantitativeValue"}
        qv.update(sal)
        ld["baseSalary"] = {"@type": "MonetaryAmount", "currency": "EUR", "value": qv}

    crumbs = {
        "@context": "https://schema.org/",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Accueil", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Offres " + (cat_label or "tech"),
             "item": "%s/emploi/%s.html" % (SITE_URL, slugify(cat or "tech"))},
            {"@type": "ListItem", "position": 3, "name": j.get("title") or "", "item": canonical},
        ],
    }

    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="../emploi/{catslug}.html">{catlabel}</a> › {title}</nav>
<h1>{title}</h1>
<p class="sub">{company}{cityline}</p>
<div class="card">
  <div class="k">{krow}</div>
  {stack}
  {apply_btn}
</div>
<div class="desc">{desc}</div>
{profile}
{benefits}
{about}
{facet_link}
{similar}
""".format(
        home=SITE_URL + "/", catslug=slugify(cat or "tech"), catlabel=esc(cat_label),
        title=esc(j.get("title")), company=co_link,
        cityline=(" — télétravail" if is_remote else (" — " + esc(city) if city else "")),
        krow=krow, stack=stack_html, apply_btn=apply_btn,
        desc=desc_html, profile=profile_html, benefits=benefits_html,
        about=about_html, facet_link=facet_link, similar=sim_html,
    )
    head_extra = jsonld(ld) + "\n" + jsonld(crumbs)
    return shell(
        title="%s — %s%s | sudtechjobs" % (
            j.get("title"), j.get("company"),
            ", " + city if city and city != "Remote" else ""),
        description=meta_desc, canonical=canonical, head_extra=head_extra, body=body,
    )


def render_tombstone(slug, e):
    """Page kept at a vanished offer's URL: no JobPosting markup, `noindex`, and a
    redirect to the matching facet list. Google for Jobs then drops the closed
    posting; a human arriving from a stale Google result still lands somewhere
    useful instead of a 404."""
    canonical = "%s/offre/%s.html" % (SITE_URL, slug)
    cat = e.get("category")
    cat_label = CATS.get(cat, (cat, cat))[0] if cat else "tech"
    # point at the broad métier facet (always built, threshold 1) rather than the
    # métier×ville one, which can fall below MIN_FACET and 404 months from now.
    facet = slugify(cat) if cat else None
    dest = ("%s/emploi/%s.html" % (SITE_URL, facet)) if facet else (SITE_URL + "/emploi/")
    title = e.get("title") or "Cette offre"
    company = e.get("company") or ""
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="{hub}">Emplois</a> › offre expirée</nav>
<h1>Offre pourvue ou expirée</h1>
<p class="sub">«&nbsp;{title}&nbsp;»{co} n'est plus en ligne.</p>
<div class="card">
  <p>Cette annonce a été retirée du board&nbsp;: la source ne la diffuse plus,
  elle a sans doute été pourvue.</p>
  <p><a class="apply" href="{dest}">Voir les offres {catlabel} toujours ouvertes →</a></p>
</div>
<p class="sub">Redirection automatique dans quelques secondes…</p>
<script>setTimeout(function(){{location.replace({dest_js})}},5000)</script>
""".format(
        home=SITE_URL + "/", hub=SITE_URL + "/emploi/",
        title=esc(title), co=(" chez " + esc(company)) if company else "",
        dest=esc(dest), dest_js=json.dumps(dest), catlabel=esc(cat_label),
    )
    head_extra = ('<meta name="robots" content="noindex, follow">\n'
                  '<meta http-equiv="refresh" content="5; url=%s">' % esc(dest))
    return shell(title="Offre expirée | sudtechjobs",
                 description="Cette offre n'est plus disponible.",
                 canonical=canonical, head_extra=head_extra, body=body)


# --------------------------------------------------------------------------- #
#  facet (list) pages                                                         #
# --------------------------------------------------------------------------- #
def job_li(j):
    tags = []
    if j.get("category"):
        tags.append(CATS.get(j["category"], (j["category"],))[0])
    if j.get("contract"):
        tags.append(j["contract"])
    if j.get("city"):
        tags.append(j["city"])
    return ('<li><a href="../offre/{slug}.html">{title}</a>'
            '<div class="co">{co}</div>'
            '<div class="k">{tags}</div></li>').format(
        slug=j["_slug"], title=esc(j.get("title")),
        co=esc(j.get("company")),
        tags="".join("<span>%s</span>" % esc(t) for t in tags))


def render_facet(*, slug, h1, intro, jobs, siblings, generated):
    canonical = "%s/emploi/%s.html" % (SITE_URL, slug)
    facet_html = ""
    if siblings:
        facet_html = '<div class="facets">%s</div>' % "".join(
            '<a href="%s.html">%s</a>' % (s, esc(label)) for s, label in siblings)
    lis = "".join(job_li(j) for j in jobs)
    ld = {
        "@context": "https://schema.org/",
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": "%s/offre/%s.html" % (SITE_URL, j["_slug"])}
            for i, j in enumerate(jobs)
        ],
    }
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="{hub}">Emplois</a> › {h1}</nav>
<h1>{h1}</h1>
<p class="sub">{intro}</p>
{facets}
<ul class="jobs">{lis}</ul>
""".format(home=SITE_URL + "/", hub=SITE_URL + "/emploi/", h1=esc(h1),
           intro=esc(intro), facets=facet_html, lis=lis)
    return shell(title="%s | sudtechjobs" % h1, description=intro,
                 canonical=canonical, head_extra=jsonld(ld), body=body)


def render_hub(groups, generated):
    canonical = SITE_URL + "/emploi/"
    sec = []
    for name, links in groups:
        if not links:
            continue
        sec.append("<h2>%s</h2>\n<div class=\"facets\">%s</div>" % (
            esc(name),
            "".join('<a href="%s.html">%s</a>' % (s, esc(l)) for s, l in links)))
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › Emplois</nav>
<h1>Emplois tech en Provence-Alpes-Côte d'Azur</h1>
<p class="sub">Offres dev, data, produit &amp; design des boîtes tech du sud, par ville
et par techno. Mis à jour le {gen}.</p>
{sec}
""".format(home=SITE_URL + "/", gen=esc(generated), sec="\n".join(sec))
    return shell(
        title="Emplois tech en PACA — par ville & techno | sudtechjobs",
        description="Toutes les offres tech du sud de la France, classées par ville "
                    "(Marseille, Aix, Sophia-Antipolis, Nice, Toulon…) et par techno.",
        canonical=canonical, body=body)


# --------------------------------------------------------------------------- #
#  legal pages  (/mentions-legales, /cgu, /confidentialite — site root)       #
# --------------------------------------------------------------------------- #
# Bump this whenever the wording below changes (the pages are rebuilt on every
# CI run but the text is static, so the date must not track the build).
LEGAL_UPDATED = "9 septembre 2026"

MENTIONS_LEGALES = """
<h2>Éditeur du site</h2>
<p>Le site <strong>sudtechjobs</strong> (<a href="https://sudtechjobs.com">sudtechjobs.com</a>)
est édité à titre non professionnel par une personne physique.</p>
<p>Conformément à l’article 6 III 2 de la loi n° 2004-575 du 21 juin 2004 pour la confiance
dans l’économie numérique (LCEN), l’éditeur, personne physique non professionnelle, a choisi
de ne pas rendre publiques ses coordonnées d’identité. Celles-ci ont été communiquées à
l’hébergeur du site, qui les tiendra à la disposition de toute autorité judiciaire qui en
ferait la demande.</p>
<p><strong>Contact :</strong>
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a></p>
<p><strong>Directeur de la publication :</strong> l’éditeur du site.</p>
<div class="note">sudtechjobs est un projet personnel et gratuit. Dès qu’une activité
commerciale sera exercée (première facturation), ces mentions seront complétées par
l’identité complète de l’éditeur — nom ou dénomination sociale, statut, numéro SIREN,
adresse — conformément à l’article 6 III 1 de la LCEN.</div>

<h2>Hébergement</h2>
<p>Le site est hébergé par <strong>GitHub, Inc.</strong> (service GitHub Pages),
88 Colin P. Kelly Jr. Street, San Francisco, CA 94107, États-Unis —
<a href="https://github.com">github.com</a>, support :
<a href="https://support.github.com">support.github.com</a>.</p>
<p>La mesure d’audience est opérée par <strong>Umami Software, Inc.</strong>
(<a href="https://umami.is">umami.is</a>). Voir la
<a href="/confidentialite.html">politique de confidentialité</a>.</p>

<h2>Origine des offres d’emploi</h2>
<p>sudtechjobs est un agrégateur. Les offres affichées sont collectées automatiquement
depuis des sources publiques : Welcome to the Jungle et les pages de recrutement (ATS)
publiques des entreprises (Ashby, Lever, SmartRecruiters, Taleez, Recruitee, Workable).
Chaque annonce renvoie vers sa source d’origine, où s’effectue la candidature.</p>
<p>L’éditeur n’est pas l’auteur des annonces. Les marques, logos, dénominations et contenus
des offres restent la propriété de leurs titulaires respectifs et ne sont repris qu’à des
fins d’information et de référencement.</p>
<p>Toute entreprise ou ayant droit peut demander la rectification ou le retrait d’une
annonce, d’un logo ou d’une fiche entreprise en écrivant à
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a> ; la demande est traitée
sous 7 jours ouvrés.</p>

<h2>Propriété intellectuelle</h2>
<p>En dehors des contenus tiers mentionnés ci-dessus, la structure du site, sa charte
graphique, ses textes de présentation et son code sont la propriété de l’éditeur. Toute
reproduction ou réutilisation non autorisée est interdite.</p>

<h2>Responsabilité</h2>
<p>Les informations sont fournies « en l’état », sans garantie d’exactitude, d’exhaustivité
ni d’actualité. Les intitulés, rémunérations, localisations, modalités de télétravail et la
disponibilité des postes dépendent des sources et doivent être vérifiés auprès de
l’employeur. L’éditeur ne saurait être tenu responsable d’un dommage lié à l’utilisation du
site ou au contenu des sites tiers vers lesquels il renvoie.</p>

<h2>Droit applicable</h2>
<p>Les présentes mentions sont soumises au droit français.</p>
"""

CGU = """
<h2>1. Objet</h2>
<p>sudtechjobs est un service gratuit d’agrégation d’offres d’emploi dans les métiers de la
tech (développement, data, produit, design) en Provence-Alpes-Côte d’Azur et dans le sud de
la France. Il référence des annonces publiées par des tiers et renvoie vers leur source
d’origine. Les présentes conditions générales d’utilisation (CGU) régissent l’accès et
l’usage du site.</p>

<h2>2. Acceptation</h2>
<p>L’utilisation du site vaut acceptation pleine et entière des présentes CGU. Si vous ne
les acceptez pas, n’utilisez pas le site.</p>

<h2>3. Accès au service</h2>
<p>Le site est fourni gratuitement et « en l’état », sans garantie de disponibilité
continue. L’éditeur peut à tout moment faire évoluer, suspendre ou interrompre tout ou
partie du service, sans préavis ni indemnité.</p>

<h2>4. Contenu des offres</h2>
<p>Les annonces sont collectées automatiquement depuis des sources publiques (Welcome to the
Jungle, pages ATS publiques des entreprises). L’éditeur n’en est pas l’auteur et ne garantit
ni leur exactitude, ni leur actualité, ni la disponibilité des postes, ni les conditions
annoncées (rémunération, lieu, télétravail, type de contrat). Aucune relation de
recrutement, de mandat, de courtage ou de placement n’est établie entre sudtechjobs et
l’utilisateur ou l’employeur.</p>

<h2>5. Candidatures</h2>
<p>Les candidatures se font exclusivement auprès de l’employeur, via le lien « postuler » de
chaque annonce. sudtechjobs ne reçoit, ne stocke et ne transmet aucun CV ni aucune
candidature.</p>

<h2>6. Obligations de l’utilisateur</h2>
<ul>
<li>utiliser le site conformément à la loi et aux présentes CGU ;</li>
<li>ne pas procéder à une extraction massive ou automatisée du contenu (<em>scraping</em>)
au-delà de ce qu’autorise le fichier <a href="/robots.txt">robots.txt</a> ;</li>
<li>ne pas porter atteinte au fonctionnement ou à la sécurité du site.</li>
</ul>

<h2>7. Propriété intellectuelle</h2>
<p>Les marques, logos et contenus des offres appartiennent à leurs titulaires respectifs.
Les autres éléments du site (structure, design, textes, code) sont la propriété de
l’éditeur. Voir les <a href="/mentions-legales.html">mentions légales</a>.</p>

<h2>8. Signalement et retrait</h2>
<p>Toute entreprise ou ayant droit peut demander la correction ou le retrait d’une annonce,
d’un logo ou d’une fiche à
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>.</p>

<h2>9. Responsabilité</h2>
<p>L’éditeur ne saurait être tenu responsable d’un préjudice direct ou indirect résultant de
l’utilisation du site, de l’indisponibilité d’une offre, d’informations erronées issues des
sources, ou du contenu des sites tiers vers lesquels le site renvoie.</p>

<h2>10. Données personnelles</h2>
<p>Le traitement des données est décrit dans la
<a href="/confidentialite.html">politique de confidentialité</a>.</p>

<h2>11. Droit applicable et litiges</h2>
<p>Les présentes CGU sont soumises au droit français. En cas de différend, une solution
amiable sera recherchée avant toute action judiciaire (contact :
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>). À défaut, les tribunaux
français sont compétents.</p>

<h2>12. Évolution des CGU</h2>
<p>Les présentes CGU peuvent être modifiées à tout moment. La version applicable est celle
en ligne à la date de votre visite.</p>
"""

CONFIDENTIALITE = """
<p>Cette politique décrit les traitements de données à caractère personnel liés au site
<strong>sudtechjobs</strong> (<a href="https://sudtechjobs.com">sudtechjobs.com</a>).</p>

<h2>Responsable de traitement</h2>
<p>L’éditeur du site (voir les <a href="/mentions-legales.html">mentions légales</a>).
Contact : <a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>.</p>

<h2>En résumé</h2>
<ul>
<li>Aucun compte, aucun formulaire, aucune inscription.</li>
<li>Aucun cookie, aucun traceur publicitaire, aucune revente de données.</li>
<li>Une mesure d’audience sans cookie et sans donnée personnelle (Umami).</li>
</ul>

<h2>Mesure d’audience (Umami)</h2>
<p>Le site utilise <strong>Umami</strong> pour comptabiliser la fréquentation. Umami
fonctionne <strong>sans cookie</strong> et sans empreinte numérique
(<em>fingerprinting</em>). Les données collectées sont agrégées et anonymes : pages vues,
site référent, pays, navigateur, système d’exploitation, type d’appareil. L’adresse IP et
l’agent utilisateur servent uniquement, de façon transitoire, à calculer un identifiant de
visite haché, renouvelé chaque jour et non réversible ; ils ne sont pas conservés.</p>
<p><strong>Base légale :</strong> intérêt légitime de l’éditeur (article 6.1.f du RGPD) à
mesurer l’audience de son site. Ce traitement, anonyme et sans cookie, ne requiert pas votre
consentement et s’inscrit dans les recommandations de la CNIL sur la mesure d’audience.</p>
<p><strong>Sous-traitant :</strong> Umami Software, Inc. —
<a href="https://umami.is/privacy">umami.is/privacy</a>.</p>

<h2>Courriels</h2>
<p>Si vous écrivez à <a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>, votre
adresse électronique et le contenu de votre message sont traités dans le seul but de vous
répondre et d’assurer le suivi de l’échange (base légale : intérêt légitime). Ces messages
sont conservés au maximum 12 mois après le dernier contact, sauf obligation légale
contraire.</p>

<h2>Cookies et stockage local</h2>
<p>Le site ne dépose <strong>aucun cookie</strong>. Il utilise une seule clé de stockage
local (<code>localStorage</code>), purement technique, pour mémoriser que vous avez fermé le
bandeau d’information. Cette donnée reste sur votre appareil et n’est jamais transmise.</p>

<h2>Hébergement et transferts hors Union européenne</h2>
<p>Le site est servi par GitHub Pages (GitHub, Inc., États-Unis). À ce titre, des journaux
techniques de serveur — dont l’adresse IP — peuvent être traités aux États-Unis par
GitHub, Inc. Ces transferts sont encadrés par les clauses contractuelles types de la
Commission européenne et/ou le <em>Data Privacy Framework</em> UE–États-Unis.</p>

<h2>Destinataires</h2>
<p>L’éditeur ; le sous-traitant de mesure d’audience (Umami) ; l’hébergeur (GitHub Pages)
pour les seuls journaux techniques. Aucune donnée n’est cédée ni louée à des tiers.</p>

<h2>Vos droits</h2>
<p>Conformément au RGPD (articles 15 à 21), vous disposez d’un droit d’accès, de
rectification, d’effacement, de limitation, d’opposition et de portabilité. Vous pouvez les
exercer à <a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>.</p>
<p>Vous pouvez introduire une réclamation auprès de la CNIL : 3 place de Fontenoy,
TSA 80715, 75334 Paris Cedex 07 — <a href="https://www.cnil.fr">www.cnil.fr</a>.</p>

<h2>Modifications</h2>
<p>Cette politique peut être mise à jour ; la version applicable est celle publiée sur cette
page, datée en tête de document.</p>
"""


def render_legal(*, slug, title, description, h1, inner):
    canonical = "%s/%s.html" % (SITE_URL, slug)
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › {h1}</nav>
<h1>{h1}</h1>
<div class="legal">
<p class="upd">Dernière mise à jour : {upd}</p>
{inner}
</div>
""".format(home=SITE_URL + "/", h1=esc(h1), upd=esc(LEGAL_UPDATED), inner=inner)
    return shell(title=title, description=description, canonical=canonical, body=body)


# --------------------------------------------------------------------------- #
#  company pages                                                              #
# --------------------------------------------------------------------------- #
CAT_LABEL = {k: v[0] for k, v in CATS.items()}


def _fmt_headcount(n):
    if not n:
        return None
    if n >= 1000:
        return "%s salarié·es" % (("%.1f" % (n / 1000)).rstrip("0").rstrip(".") + " k")
    return "%d salarié·es" % n


def _company_list(jobs):
    return '<ul class="jobs">%s</ul>' % "".join(
        '<li><a href="../offre/{s}.html"><span class="t">{t}</span>'
        '<span class="co">{meta}</span></a></li>'.format(
            s=j["_slug"], t=esc(j.get("title")),
            meta=esc(" · ".join(x for x in [
                CAT_LABEL.get(j.get("category"), j.get("category")),
                j.get("contract"),
                "télétravail" if (j.get("city") == "Remote") else j.get("city"),
            ] if x)))
        for j in jobs)


def render_company(rec, jobs, generated, live_facets):
    slug = rec["slug"]
    name = rec["name"]
    canonical = "%s/entreprise/%s.html" % (SITE_URL, slug)
    p = rec.get("profile") or {}
    city = rec.get("city") or ""
    n = rec.get("open_roles", len(jobs))

    # ---- header: cover banner + big logo straddling it, then badges --------
    cover = ('<div class="cover" style="background-image:url(%s)"></div>' % esc(p["cover_image"])) \
        if p.get("cover_image") else ""
    if rec.get("logo"):
        logo_html = '<img class="lg" src="%s" alt="%s" loading="lazy">' % (esc(rec["logo"]), esc(name))
    else:
        logo_html = '<div class="lg ph">%s</div>' % esc((name or "?")[:1])

    badges = []
    for eco in rec.get("ecosystems") or []:
        badges.append('<span class="eco">%s</span>' % esc(eco))
    for s in (p.get("sectors") or [])[:3]:
        badges.append("<span>%s</span>" % esc(s))
    for t in (rec.get("tags") or [])[:4]:
        if not p.get("sectors"):
            badges.append("<span>%s</span>" % esc(t))
    badges_html = ('<div class="badges">%s</div>' % "".join(badges)) if badges else ""

    # ---- facts grid ----------------------------------------------------------
    facts = []

    def fact(dt, dd):
        if dd:
            facts.append("<div><dt>%s</dt><dd>%s</dd></div>" % (esc(dt), dd))

    fact("Postes ouverts", str(n))
    fact("Type", esc(rec.get("type")) if rec.get("type") else None)
    fact("Effectif", esc(_fmt_headcount(p.get("headcount"))) if p.get("headcount") else None)
    fact("Création", esc(p.get("founded")) if p.get("founded") else None)
    hq = p.get("hq_city")
    if hq and hq.lower() != (city or "").lower():
        fact("Siège", esc(hq))
    fact("Sur le board", esc(city) if city else None)
    if p.get("parity_women") is not None:
        fact("Parité F/H", "%s%% / %s%%" % (esc(p.get("parity_women")), esc(p.get("parity_men"))))
    if p.get("equality_index") is not None:
        fact("Index égalité", "%s/100" % esc(p["equality_index"]))
    if rec.get("experience_min_years") is not None:
        fact("Exp. moyenne demandée", "%s ans" % esc(rec["experience_min_years"]))
    if rec.get("ats"):
        fact("Recrutement via", esc(str(rec["ats"]).title()))
    dom = p.get("socials", {}).get("website") or (
        ("https://" + rec["domain"]) if rec.get("domain") else None)
    if dom:
        fact("Site", '<a href="%s" target="_blank" rel="nofollow noopener">%s</a>'
             % (esc(dom), esc(re.sub(r"^https?://(www\.)?", "", dom).rstrip("/"))))
    if rec.get("careers_url"):
        fact("Page carrières", '<a href="%s" target="_blank" rel="nofollow noopener">voir</a>'
             % esc(rec["careers_url"]))
    if p.get("wttj_url"):
        fact("Fiche WTTJ", '<a href="%s" target="_blank" rel="nofollow noopener">'
             "Welcome to the Jungle</a>" % esc(p["wttj_url"]))
    facts_html = ('<dl class="facts">%s</dl>' % "".join(facts)) if facts else ""

    SOCIAL_LABEL = {"linkedin": "LinkedIn", "twitter": "X / Twitter", "instagram": "Instagram",
                    "youtube": "YouTube", "facebook": "Facebook"}
    social = "".join(
        '<a href="%s" target="_blank" rel="nofollow noopener">%s</a>' % (esc(u), esc(lbl))
        for k, lbl in SOCIAL_LABEL.items()
        for u in [p.get("socials", {}).get(k)] if u)
    social_html = ('<div class="social">%s</div>' % social) if social else ""

    desc_html = ""
    if p.get("description"):
        desc_html = '<div class="desc">%s</div>' % text_to_html(p["description"])

    # ---- hiring rhythm sparkline ------------------------------------------
    hm = rec.get("hiring_months") or {}
    counts = hm.get("counts") or []
    spark = ""
    # only worth a chart once there's a bit of signal (≥1 non-empty month among
    # several, or a real backlog) — a lone bar on a 1-role company says nothing
    if sum(1 for c in counts if c) >= 2 or rec.get("posted_90d", 0) >= 5:
        mx = max(counts) or 1
        bars = "".join(
            '<i class="%s" style="height:%d%%" title="%s : %d"></i>' % (
                "now" if i == len(counts) - 1 else "", max(4, round(c / mx * 100)),
                esc((hm.get("labels") or [""] * len(counts))[i]), c)
            for i, c in enumerate(counts))
        rhythm = "%d offre%s repérée%s ces 90 jours" % (
            rec.get("posted_90d", 0), "s" if rec.get("posted_90d", 0) > 1 else "",
            "s" if rec.get("posted_90d", 0) > 1 else "")
        spark = ('<h2>Rythme de recrutement</h2>\n<div class="spark">%s</div>'
                 '<p class="sub">%s · sur 12 mois (première apparition sur le board).</p>'
                 % (bars, esc(rhythm)))

    # ---- stack cloud (links to techno facets when they exist) -----------
    stack_html = ""
    if rec.get("stack"):
        chips = []
        for item in rec["stack"]:
            s = item["name"]
            fslug = "stack-%s" % slugify(s)
            label = "%s<span style='opacity:.55'> ·%d</span>" % (esc(s), item["n"])
            if fslug in live_facets:
                chips.append('<a href="../emploi/%s.html"><b>%s</b></a>' % (fslug, label))
            else:
                chips.append("<b>%s</b>" % label)
        stack_html = ('<h2>Stack technique</h2>\n<div class="stack">%s</div>'
                      % "".join(chips))

    # ---- breakdowns (métier / contrat / ville) --------------------------
    def mini(pairs, href=None):
        out = []
        for lbl, cnt in pairs:
            txt = "%s · %d" % (esc(lbl), cnt)
            h = href(lbl) if href else None
            out.append('<a href="../emploi/%s.html">%s</a>' % (h, txt) if h and h in live_facets
                       else "<span>%s</span>" % txt)
        return '<div class="mini">%s</div>' % "".join(out)

    bre = []
    if rec.get("by_category"):
        brec = [(CAT_LABEL.get(k, k), v) for k, v in rec["by_category"].items()]
        brec_href = None
        # a métier facet is <cat-slug>; métier×ville is <cat>-<city>
        cat_by_label = {CAT_LABEL.get(k, k): k for k in rec["by_category"]}
        brec_href = lambda lbl: slugify(cat_by_label.get(lbl, lbl))
        bre.append("<h2>Par métier</h2>\n" + mini(brec, brec_href))
    if rec.get("by_contract"):
        bre.append("<h2>Par contrat</h2>\n" + mini(list(rec["by_contract"].items())))
    if rec.get("by_city"):
        bre.append("<h2>Où ils recrutent</h2>\n"
                   + mini(list(rec["by_city"].items()), lambda c: slugify(c)))
    if rec.get("remote_roles"):
        bre.append('<p class="sub">%d poste%s ouvert%s au télétravail.</p>' % (
            rec["remote_roles"], "s" if rec["remote_roles"] > 1 else "",
            "s" if rec["remote_roles"] > 1 else ""))
    breakdown_html = "\n".join(bre)

    sal_html = ""
    if rec.get("salary_samples"):
        sal_html = ("<h2>Salaires affichés</h2>\n<div class=\"k\">%s</div>" % "".join(
            '<span class="sal">%s</span>' % esc(s) for s in rec["salary_samples"]))

    benefits_html = ""
    if rec.get("benefits"):
        benefits_html = ('<h2>Avantages mentionnés dans les offres</h2>\n<div class="mini">%s</div>'
                         % "".join("<span>%s</span>" % esc(b) for b in rec["benefits"]))

    jobs_sorted = sorted(jobs, key=lambda j: (j.get("first_seen") or "", j.get("published_at") or ""),
                         reverse=True)
    jobs_html = "<h2>Offres ouvertes (%d)</h2>\n%s" % (len(jobs_sorted), _company_list(jobs_sorted))

    # ---- JSON-LD -------------------------------------------------------------
    org_ld = {"@context": "https://schema.org", "@type": "Organization", "name": name,
              "url": dom or canonical}
    if rec.get("logo"):
        org_ld["logo"] = rec["logo"]
    if p.get("description"):
        org_ld["description"] = p["description"]
    same_as = [u for u in p.get("socials", {}).values() if u]
    if same_as:
        org_ld["sameAs"] = same_as
    if p.get("headcount"):
        org_ld["numberOfEmployees"] = {"@type": "QuantitativeValue", "value": p["headcount"]}
    if p.get("founded"):
        org_ld["foundingDate"] = str(p["founded"])
    if p.get("hq_city"):
        org_ld["address"] = {"@type": "PostalAddress", "addressLocality": p["hq_city"],
                             "addressCountry": p.get("hq_country") or "FR"}
    list_ld = {
        "@context": "https://schema.org", "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "url": "%s/offre/%s.html" % (SITE_URL, j["_slug"])}
            for i, j in enumerate(jobs_sorted)],
    }
    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Accueil", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Entreprises",
             "item": SITE_URL + "/entreprise/"},
            {"@type": "ListItem", "position": 3, "name": name, "item": canonical},
        ],
    }

    where = (" à " + esc(city)) if city else " en PACA"
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="{hub}">Entreprises</a> › {name}</nav>
{cover}
<div class="cohead">{logo}
  <div><h1>Emplois tech chez {name}</h1>
  <p class="sub" style="margin:0">{n} offre{s} dev · data · produit · design{where}.</p></div>
</div>
{badges}
<div class="card">
  {facts}
  {social}
  {desc}
</div>
{spark}
{stack}
{breakdown}
{benefits}
{sal}
{jobs}
<p class="sub" style="margin-top:22px">Données agrégées depuis les offres publiées, mises à jour le {gen}.
Une info à corriger ? <a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a></p>
""".format(
        home=SITE_URL + "/", hub=SITE_URL + "/entreprise/", name=esc(name),
        cover=cover, logo=logo_html,
        n=n, s="s" if n > 1 else "", where=where,
        badges=badges_html, facts=facts_html, social=social_html, desc=desc_html,
        spark=spark, stack=stack_html, breakdown=breakdown_html,
        benefits=benefits_html, sal=sal_html, jobs=jobs_html, gen=esc(generated),
    )
    meta = "%s recrute : %d offre%s tech (dev, data, produit, design)%s sur sudtechjobs." % (
        name, n, "s" if n > 1 else "", where)
    if p.get("description"):
        meta = re.sub(r"\s+", " ", p["description"])[:180]
    head_extra = jsonld(org_ld) + "\n" + jsonld(list_ld) + "\n" + jsonld(crumbs)
    return shell(title="%s — emplois tech%s | sudtechjobs" % (name, where),
                 description=meta, canonical=canonical, head_extra=head_extra, body=body)


def render_companies_hub(companies, generated):
    canonical = SITE_URL + "/entreprise/"
    rows = "".join(
        '<li><a href="{s}.html">{n}</a><div class="co">{meta}</div></li>'.format(
            s=c["slug"], n=esc(c["name"]),
            meta=esc(" · ".join(x for x in [
                "%d offre%s" % (c["open_roles"], "s" if c["open_roles"] > 1 else ""),
                c.get("city") or "",
                (c.get("profile") or {}).get("sectors", [None])[0] or "",
                (c.get("ecosystems") or [None])[0] or "",
            ] if x)))
        for c in companies)
    ld = {
        "@context": "https://schema.org", "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": c["name"],
             "url": "%s/entreprise/%s.html" % (SITE_URL, c["slug"])}
            for i, c in enumerate(companies)],
    }
    total = sum(c["open_roles"] for c in companies)
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › Entreprises</nav>
<h1>Les entreprises qui recrutent dans la tech en PACA</h1>
<p class="sub">{nc} entreprises, {total} offres dev, data, produit &amp; design dans le sud
de la France. Mis à jour le {gen}.</p>
<ul class="jobs">{rows}</ul>
""".format(home=SITE_URL + "/", nc=len(companies), total=total, gen=esc(generated), rows=rows)
    return shell(
        title="Entreprises tech qui recrutent en PACA | sudtechjobs",
        description="Toutes les boîtes tech du sud de la France qui recrutent : "
                    "effectif, secteur, stack, offres ouvertes. Marseille, Aix, Nice, "
                    "Sophia-Antipolis, Toulon, Avignon.",
        canonical=canonical, head_extra=jsonld(ld), body=body)


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #
def wipe_html(dirpath, keep):
    if not os.path.isdir(dirpath):
        return
    for fn in os.listdir(dirpath):
        if fn.endswith(".html") and fn not in keep:
            os.remove(os.path.join(dirpath, fn))


def main():
    if not os.path.exists(FEED):
        sys.exit("no %s — run jobboard/build.py first" % FEED)
    feed = json.load(open(FEED, encoding="utf-8"))
    jobs = feed.get("jobs", [])
    if not jobs:
        sys.exit("feed has no jobs")
    generated = (feed.get("generated_at") or "")[:10]

    for j in jobs:
        j["_slug"] = j.get("slug") or job_slug(j)   # build.py stamps `slug`; recompute if absent

    # company rows (build_companies.py). Optional: without it the offer/facet
    # pages still render, just with no company pages and no cross-links.
    companies = []
    try:
        companies = json.load(open(COMPANIES, encoding="utf-8")).get("companies", [])
    except (OSError, ValueError):
        print("render_pages: no %s — skipping company pages "
              "(run jobboard/build_companies.py)" % COMPANIES, file=sys.stderr)
    company_by_name = {c["name"]: c for c in companies}
    for j in jobs:
        c = company_by_name.get(j.get("company"))
        if c:
            j["_company_slug"] = c["slug"]
            j["_company"] = c

    offre_dir = os.path.join(SITE, "offre")
    emploi_dir = os.path.join(SITE, "emploi")
    entreprise_dir = os.path.join(SITE, "entreprise")
    os.makedirs(offre_dir, exist_ok=True)
    os.makedirs(emploi_dir, exist_ok=True)
    os.makedirs(entreprise_dir, exist_ok=True)

    # ---- facets ----------------------------------------------------------
    # slug -> dict(h1, intro, test, kind)
    facets = {}

    def add_facet(slug, h1, intro, test, kind):
        facets[slug] = {"h1": h1, "intro": intro, "test": test, "kind": kind}

    cities = sorted({j["city"] for j in jobs if j.get("city") and j["city"] != "Remote"})
    stacks = {}
    for j in jobs:
        for s in (j.get("stack") or []):
            stacks[s] = stacks.get(s, 0) + 1

    for cat, (label, _short) in CATS.items():
        add_facet(slugify(cat), "Emplois %s en PACA" % label,
                  "Postes %s dans les entreprises tech de Provence-Alpes-Côte d'Azur." % label,
                  (lambda c: (lambda j: j.get("category") == c))(cat), "métier")

    for city in cities:
        add_facet(slugify(city), "Emplois tech à %s" % city,
                  "Offres dev, data, produit & design à %s et alentours." % city,
                  (lambda c: (lambda j: j.get("city") == c))(city), "ville")

    # ---- département roll-ups (PACA) ------------------------------------
    # One page per département, aggregating its cities. Thin ones (04/05 and
    # usually 84) fall below MIN_FACET and are dropped by the live_facets gate.
    seen_depts = {CITY_DEPT.get(slugify(c)) for c in cities}
    unknown = sorted({c for c in cities if slugify(c) not in CITY_DEPT})
    if unknown:
        print("render_pages: cities with no département mapping: %s"
              % ", ".join(unknown), file=sys.stderr)
    for code, (dslug, _dname, dwhere) in PACA_DEPTS.items():
        if code not in seen_depts:
            continue
        add_facet(
            "dept-%s" % dslug,
            "Emplois tech %s" % dwhere,
            "Toutes les offres dev, data, produit & design des entreprises tech "
            "%s (%s) et de ses villes." % (dwhere, code),
            (lambda cd: (lambda j: CITY_DEPT.get(slugify(j.get("city") or "")) == cd))(code),
            "département")

    for cat, (label, _short) in CATS.items():
        for city in cities:
            n = sum(1 for j in jobs if j.get("category") == cat and j.get("city") == city)
            if n >= MIN_FACET:
                add_facet(
                    "%s-%s" % (slugify(cat), slugify(city)),
                    "Emplois %s à %s" % (label, city),
                    "Postes %s à %s : %d offres des boîtes tech du sud." % (label, city, n),
                    (lambda c, ci: (lambda j: j.get("category") == c and j.get("city") == ci))(cat, city),
                    "métier×ville")

    top_stacks = [s for s, n in sorted(stacks.items(), key=lambda x: -x[1])
                  if n >= MIN_STACK and slugify(s) not in STACK_DENY]
    for s in top_stacks:
        add_facet("stack-%s" % slugify(s), "Emplois %s en PACA" % s,
                  "Offres tech mentionnant %s dans le sud de la France." % s,
                  (lambda st: (lambda j: st in (j.get("stack") or [])))(s), "techno")
        for city in cities:
            n = sum(1 for j in jobs if s in (j.get("stack") or []) and j.get("city") == city)
            if n >= MIN_FACET:
                add_facet(
                    "stack-%s-%s" % (slugify(s), slugify(city)),
                    "Emplois %s à %s" % (s, city),
                    "Postes tech %s à %s : %d offres." % (s, city, n),
                    (lambda st, ci: (lambda j: st in (j.get("stack") or []) and j.get("city") == ci))(s, city),
                    "techno×ville")

    add_facet("teletravail", "Emplois tech en télétravail dans le sud",
              "Offres full remote et télétravail des entreprises tech de PACA.",
              lambda j: j.get("city") == "Remote" or (j.get("remote") or "") in REMOTE_FULL,
              "télétravail")
    for cat, (label, _short) in CATS.items():
        n = sum(1 for j in jobs
                if (j.get("category") == cat)
                and (j.get("city") == "Remote" or (j.get("remote") or "") in REMOTE_FULL))
        if n >= MIN_FACET:
            add_facet("%s-teletravail" % slugify(cat),
                      "Emplois %s en télétravail" % label,
                      "Postes %s full remote / télétravail, entreprises tech du sud." % label,
                      (lambda c: (lambda j: j.get("category") == c and (
                          j.get("city") == "Remote" or (j.get("remote") or "") in REMOTE_FULL)))(cat),
                      "métier×télétravail")

    # resolve which facets actually have enough jobs; attach jobs
    live_facets = {}
    for slug, f in facets.items():
        matched = [j for j in jobs if f["test"](j)]
        threshold = 1 if f["kind"] == "métier" else MIN_FACET
        if len(matched) >= threshold:
            matched.sort(key=lambda j: (j.get("first_seen") or "", j.get("published_at") or ""),
                         reverse=True)
            f["jobs"] = matched
            live_facets[slug] = f

    # give each job a link to its best "métier×ville" facet (for the offer page)
    for j in jobs:
        cand = "%s-%s" % (slugify(j.get("category") or ""), slugify(j.get("city") or ""))
        j["_facet_slug"] = cand if cand in live_facets else (
            slugify(j.get("category") or "") if slugify(j.get("category") or "") in live_facets else None)

    # ---- write offer pages --------------------------------------------------
    by_cat_city = {}
    by_company = {}
    for j in jobs:
        by_cat_city.setdefault((j.get("category"), j.get("city")), []).append(j)
        ckey = (j.get("company") or "").strip().lower()
        if ckey:
            by_company.setdefault(ckey, []).append(j)

    offer_files = set()
    for j in jobs:
        ckey = (j.get("company") or "").strip().lower()
        # other openings at the same company (shown in a dedicated block)
        same_co = [s for s in by_company.get(ckey, []) if s is not j][:8]
        same_co_ids = {id(s) for s in same_co}
        # "similaires" = same métier×ville, but not the same company (that has its own block)
        sims = [s for s in by_cat_city.get((j.get("category"), j.get("city")), [])
                if s is not j and id(s) not in same_co_ids][:5]
        fn = j["_slug"] + ".html"
        offer_files.add(fn)
        with open(os.path.join(offre_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(render_offer(j, sims, same_co))

    # ---- expired-offer hygiene -----------------------------------------------
    # Track every slug we have ever published. When one drops out of the feed,
    # keep its URL alive as a `noindex` tombstone (redirect to the facet) for
    # TOMBSTONE_DAYS, then let it 404. Tombstones are NOT put in the sitemap.
    today = datetime.now(timezone.utc).date().isoformat()
    tomb_cutoff = (datetime.now(timezone.utc)
                   - timedelta(days=TOMBSTONE_DAYS)).date().isoformat()
    try:
        oindex = json.load(open(OFFER_INDEX, encoding="utf-8"))
    except (OSError, ValueError):
        oindex = {}

    live_slugs = set()
    for j in jobs:
        s = j["_slug"]
        live_slugs.add(s)
        e = oindex.get(s) or {}
        e.update({
            "title": j.get("title"), "company": j.get("company"),
            "city": j.get("city"), "category": j.get("category"),
            "facet_slug": j.get("_facet_slug"),
            "last_seen": today, "expired_at": None,
        })
        e.setdefault("first_seen",
                     (j.get("first_seen") or j.get("published_at") or today)[:10])
        oindex[s] = e

    for s, e in oindex.items():
        if s not in live_slugs and not e.get("expired_at"):
            e["expired_at"] = today

    tombstone_files = set()
    for s in list(oindex):
        e = oindex[s]
        exp = e.get("expired_at")
        if not exp:
            continue
        if exp < tomb_cutoff:
            del oindex[s]                       # long gone — forget it, let it 404
            continue
        fn = s + ".html"
        if fn in offer_files:                   # somehow back in the feed — skip
            continue
        tombstone_files.add(fn)
        with open(os.path.join(offre_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(render_tombstone(s, e))

    os.makedirs(DATA, exist_ok=True)
    with open(OFFER_INDEX, "w", encoding="utf-8") as fh:
        json.dump(oindex, fh, ensure_ascii=False, indent=0, sort_keys=True)

    # ---- write facet pages ------------------------------------------------
    FAMILY = {
        "métier": ("métier", "métier×ville"),
        "métier×ville": ("métier", "métier×ville"),
        "ville": ("ville", "département"),
        "département": ("département", "ville"),
        "techno": ("techno", "techno×ville"),
        "techno×ville": ("techno", "techno×ville"),
        "télétravail": ("télétravail", "métier×télétravail"),
        "métier×télétravail": ("télétravail", "métier×télétravail"),
    }

    def siblings_for(slug, f):
        want = FAMILY.get(f["kind"], (f["kind"],))
        toks = set(slug.split("-"))
        fam = [(s2, f2["h1"].replace("Emplois ", "")
                              .replace("tech dans ", "").replace("tech à ", ""))
               for s2, f2 in live_facets.items()
               if s2 != slug and f2["kind"] in want]
        # related first (shares a city / techno / métier token with this slug)
        fam.sort(key=lambda sl: (-len(toks & set(sl[0].split("-"))), sl[1]))
        return fam[:12]

    facet_files = set()
    for slug, f in live_facets.items():
        fn = slug + ".html"
        facet_files.add(fn)
        with open(os.path.join(emploi_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(render_facet(slug=slug, h1=f["h1"], intro=f["intro"],
                                  jobs=f["jobs"], siblings=siblings_for(slug, f),
                                  generated=generated))

    # ---- hub -------------------------------------------------------------
    def grp(kinds, strip):
        return sorted(
            ((s, f["h1"].replace(strip, "")) for s, f in live_facets.items() if f["kind"] in kinds),
            key=lambda x: x[1])
    groups = [
        ("Par ville", grp(("ville",), "Emplois tech à ")),
        ("Par département", grp(("département",), "Emplois tech dans ")),
        ("Par métier", grp(("métier", "métier×ville"), "Emplois ")),
        ("Par techno", grp(("techno", "techno×ville"), "Emplois ")),
        ("Télétravail", grp(("télétravail", "métier×télétravail"), "Emplois ")),
    ]
    with open(os.path.join(emploi_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_hub(groups, generated))
    facet_files.add("index.html")

    # ---- company pages --------------------------------------------------
    jobs_by_company = {}
    for j in jobs:
        jobs_by_company.setdefault(j.get("company"), []).append(j)
    company_files = set()
    for c in companies:
        cjobs = jobs_by_company.get(c["name"]) or []
        if not cjobs:
            continue
        fn = c["slug"] + ".html"
        company_files.add(fn)
        with open(os.path.join(entreprise_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(render_company(c, cjobs, generated, live_facets))
    if companies:
        live_companies = [c for c in companies if (c["slug"] + ".html") in company_files]
        with open(os.path.join(entreprise_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(render_companies_hub(live_companies, generated))
        company_files.add("index.html")

    # ---- legal pages (site root) ----------------------------------------
    legal = [
        ("mentions-legales", "Mentions légales | sudtechjobs",
         "Mentions légales de sudtechjobs : éditeur, hébergeur, origine des offres, "
         "propriété intellectuelle, responsabilité.",
         "Mentions légales", MENTIONS_LEGALES),
        ("cgu", "Conditions générales d’utilisation | sudtechjobs",
         "Conditions générales d’utilisation de sudtechjobs, agrégateur gratuit d’offres "
         "d’emploi tech dans le sud de la France.",
         "Conditions générales d’utilisation", CGU),
        ("confidentialite", "Politique de confidentialité | sudtechjobs",
         "Politique de confidentialité (RGPD) de sudtechjobs : mesure d’audience sans "
         "cookie, données traitées, vos droits.",
         "Politique de confidentialité", CONFIDENTIALITE),
    ]
    for slug, title, desc, h1, inner in legal:
        with open(os.path.join(SITE, slug + ".html"), "w", encoding="utf-8") as fh:
            fh.write(render_legal(slug=slug, title=title, description=desc,
                                  h1=h1, inner=inner))

    # ---- sitemaps + robots -------------------------------------------------
    # A sitemap index pointing at two children: the browse pages, and a dedicated
    # offers sitemap (live postings only — Google for Jobs discovers JobPosting
    # pages from this one). Tombstones and 404s stay out of both.
    def _urlset(entries):
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "\n".join(entries) + "\n</urlset>\n")

    pages = ['<url><loc>%s/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>'
             % SITE_URL,
             '<url><loc>%s/emploi/</loc><changefreq>daily</changefreq><priority>0.8</priority></url>'
             % SITE_URL]
    for slug in ("mentions-legales", "cgu", "confidentialite"):
        pages.append('<url><loc>%s/%s.html</loc><changefreq>yearly</changefreq>'
                     '<priority>0.2</priority></url>' % (SITE_URL, slug))
    for slug in sorted(live_facets):
        pages.append('<url><loc>%s/emploi/%s.html</loc><lastmod>%s</lastmod>'
                     '<changefreq>daily</changefreq><priority>0.7</priority></url>'
                     % (SITE_URL, slug, today))
    if companies:
        pages.append('<url><loc>%s/entreprise/</loc><lastmod>%s</lastmod>'
                     '<changefreq>daily</changefreq><priority>0.7</priority></url>'
                     % (SITE_URL, today))
        for fn in sorted(company_files):
            if fn == "index.html":
                continue
            pages.append('<url><loc>%s/entreprise/%s</loc><lastmod>%s</lastmod>'
                         '<changefreq>weekly</changefreq><priority>0.6</priority></url>'
                         % (SITE_URL, fn, today))

    offers = []
    for j in jobs:
        lm = (j.get("first_seen") or j.get("published_at") or today)[:10]
        offers.append('<url><loc>%s/offre/%s.html</loc><lastmod>%s</lastmod>'
                      '<changefreq>daily</changefreq><priority>0.6</priority></url>'
                      % (SITE_URL, j["_slug"], lm))

    with open(os.path.join(SITE, "sitemap-pages.xml"), "w", encoding="utf-8") as fh:
        fh.write(_urlset(pages))
    with open(os.path.join(SITE, "sitemap-offres.xml"), "w", encoding="utf-8") as fh:
        fh.write(_urlset(offers))
    index = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
             '<sitemap><loc>%s/sitemap-pages.xml</loc><lastmod>%s</lastmod></sitemap>\n'
             '<sitemap><loc>%s/sitemap-offres.xml</loc><lastmod>%s</lastmod></sitemap>\n'
             '</sitemapindex>\n' % (SITE_URL, today, SITE_URL, today))
    with open(os.path.join(SITE, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write(index)
    with open(os.path.join(SITE, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % SITE_URL)

    # ---- prune stale files -------------------------------------------------
    wipe_html(offre_dir, offer_files | tombstone_files)
    wipe_html(emploi_dir, facet_files)
    wipe_html(entreprise_dir, company_files)

    print("render_pages: %d offers, %d tombstones, %d facet pages, %d company pages, "
          "%d sitemap urls"
          % (len(offer_files), len(tombstone_files), len(facet_files),
             len(company_files), len(pages) + len(offers)), file=sys.stderr)


if __name__ == "__main__":
    main()
