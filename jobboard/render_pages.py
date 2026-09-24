#!/usr/bin/env python3
"""Layer 5 — static SEO pages.

Reads jobboard/site/jobs.json (built by build.py) and writes, into jobboard/site/:

    offre/<slug>.html     one page per job — JobPosting JSON-LD, OpenGraph, canonical
                          (or a noindex tombstone once the offer leaves the feed)
    emploi/<facet>.html   filtered list pages (métier × ville, techno × ville, télétravail…)
    emploi/index.html     hub linking every facet page
    sitemap.xml           sitemap index -> sitemap-pages.xml + sitemap-offres.xml
    feed.xml              RSS 2.0 of the 50 newest postings (Slack/Discord bots, social auto-post)
    feed-dept-<dept>.xml  same, scoped to one PACA département (partner imports, e.g. a
                          French Tech chapter re-feeding its own stale job page)
    robots.txt            points crawlers at the sitemap index
    llms.txt              curated site map for LLM agents (llmstxt.org)

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
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

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


def _logo_html(src, name, css_class):
    """<img> with a `css_class` + `css_class ph` (letter placeholder) fallback.
    Some logos (e.g. the Clearbit-by-domain fallback for ATS-only companies,
    see build_companies.py) can 404 for a given domain — swap to the letter
    tile on error instead of leaving a broken image icon."""
    letter = (name or "?")[:1]
    if not src:
        return '<div class="%s ph">%s</div>' % (css_class, esc(letter))
    onerror = ("this.replaceWith(Object.assign(document.createElement('div'),"
               "{className:%s,textContent:%s}))") % (
        json.dumps("%s ph" % css_class), json.dumps(letter))
    return '<img class="%s" src="%s" alt="%s" loading="lazy" onerror="%s">' % (
        css_class, esc(src), esc(name), esc(onerror))


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
  --tag-eng:#2C6C9E; --tag-data:#7B4BD8; --tag-product:#2C9A6B; --tag-design:#D6455D; --tag-adj:#8A6D1F;
  --remote:#0E8FA8;
  --shadow:0 1px 2px rgba(22,48,63,.05), 0 12px 28px -16px rgba(22,48,63,.18);
  --iris:
    radial-gradient(105% 70% at 96% -8%,  color-mix(in srgb,var(--accent) 22%,transparent) 0%, transparent 55%),
    radial-gradient(105% 80% at -8% -4%,  color-mix(in srgb,var(--brand) 46%,transparent) 0%, transparent 52%),
    radial-gradient(120% 55% at 50% 118%, color-mix(in srgb,var(--brand) 16%,transparent) 0%, transparent 60%);
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
/* direct-child combinator: this is the plain "whole card is one <a>" pattern
   (companies hub, offer page's "similaires" list) — must NOT leak into the
   richer .job card below, whose <a> tags sit several levels deep */
ul.jobs li > a{display:block;padding:13px 15px;font-family:"Bricolage Grotesque",sans-serif;
 font-weight:600;color:var(--ink);font-size:15px}
ul.jobs li > a:hover{text-decoration:none;background:var(--card-2)}
ul.jobs .co{display:block;color:var(--muted);font-size:13px;font-weight:400;margin:3px 0 0;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* rich job card (job_li / _company_job_li) — same anatomy as the homepage's
   .job card, so a facet/company page doesn't look like a stripped-down site */
ul.jobs li.job{overflow:visible;padding:16px 18px;position:relative;
 transition:border-color .12s ease,transform .12s ease}
ul.jobs li.job:hover{border-color:var(--brand);transform:translateY(-1px)}
ul.jobs li.job .head{display:flex;gap:13px;align-items:flex-start}
ul.jobs li.job .head .info{min-width:0;flex:1}
ul.jobs li.job .logo{width:44px;height:44px;border-radius:11px;object-fit:contain;background:var(--card);
 border:1px solid var(--line);flex:none}
ul.jobs li.job .logo.ph{display:flex;align-items:center;justify-content:center;
 font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:17px;color:var(--brand-ink);
 background:color-mix(in srgb,var(--brand) 22%,transparent);
 border-color:color-mix(in srgb,var(--brand) 34%,transparent)}
ul.jobs li.job .t{font-family:"Bricolage Grotesque",sans-serif;font-size:16px;font-weight:600;
 letter-spacing:-.01em;line-height:1.3}
ul.jobs li.job .t a{color:var(--ink)}
ul.jobs li.job .t a::after{content:"";position:absolute;inset:0}
ul.jobs li.job .co{display:block;color:var(--muted);font-size:13px;margin:3px 0 0;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
ul.jobs li.job .co b{color:var(--ink);font-weight:600}
ul.jobs li.job .co b a{position:relative;z-index:1;color:var(--brand-ink);text-decoration:underline;
 text-decoration-color:color-mix(in srgb,var(--brand-ink) 35%,transparent);text-underline-offset:2px}
ul.jobs li.job .co b a:hover{text-decoration-color:currentColor}
ul.jobs li.job.co-job .t{margin-top:0}
ul.jobs li.job .meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px;font-size:12px;
 color:var(--muted);align-items:center}
ul.jobs li.job .meta span{background:var(--card-2);border:1px solid var(--line);border-radius:6px;
 padding:3px 8px;white-space:nowrap}
ul.jobs li.job .meta .sal{color:var(--pine);border-color:color-mix(in srgb,var(--pine) 35%,transparent);
 background:color-mix(in srgb,var(--pine) 12%,transparent);font-weight:500}
ul.jobs li.job .meta .new{color:var(--accent-ink);border-color:color-mix(in srgb,var(--accent) 42%,transparent);
 background:color-mix(in srgb,var(--accent) 16%,transparent);font-weight:600;font-family:"IBM Plex Mono",monospace}
ul.jobs li.job .meta .tag{font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:600;
 text-transform:uppercase;letter-spacing:.04em;padding:3px 8px;border-radius:6px;color:#fff;border:0}
ul.jobs li.job .meta .tag.eng{background:var(--tag-eng)}
ul.jobs li.job .meta .tag.data{background:var(--tag-data)}
ul.jobs li.job .meta .tag.product{background:var(--tag-product)}
ul.jobs li.job .meta .tag.design{background:var(--tag-design)}
ul.jobs li.job .meta .tag.tech-adjacent{background:var(--tag-adj)}
ul.jobs li.job .meta .remote{color:#fff;background:var(--remote);border-color:var(--remote);
 font-weight:600;font-family:"IBM Plex Mono",monospace;letter-spacing:.01em;text-transform:lowercase}
/* city facet hero — a designed banner (no stock photo pipeline yet): a tinted
   gradient + abstract skyline, hue-rotated per city so each town reads distinct */
.city-hero{position:relative;overflow:hidden;border-radius:16px;margin:14px 0 18px;
 padding:28px 22px 22px;min-height:104px;display:flex;align-items:flex-end;
 background:linear-gradient(175deg,color-mix(in srgb,var(--brand) 32%,var(--bg)) 0%,var(--bg) 100%);
 border:1px solid var(--line)}
.city-hero .skyline{position:absolute;inset:0;width:100%;height:100%;
 filter:hue-rotate(var(--hue,0deg));opacity:.55}
.city-hero .skyline rect,.city-hero .skyline circle{fill:var(--brand-ink)}
.city-hero-text{position:relative;z-index:1}
.city-hero .eyebrow{display:block;font-family:"IBM Plex Mono",monospace;font-size:11px;
 letter-spacing:.08em;text-transform:uppercase;color:var(--brand-ink);margin:0 0 4px;font-weight:600}
.city-hero h1{margin:0}
footer{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
footer .social{display:flex;gap:10px;margin:14px 0 0}
footer .social a{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
 border:1px solid var(--line);border-radius:8px;color:var(--muted);background:var(--card)}
footer .social a:hover{color:var(--brand-ink);border-color:var(--brand);text-decoration:none}
footer .social svg{width:15px;height:15px;fill:currentColor}
/* company page */
.cover{height:150px;border-radius:14px;background:var(--card-2) center/cover no-repeat;
 border:1px solid var(--line);margin:8px 0 12px}
.cover.cover-fallback{background:
   radial-gradient(120% 140% at 8% -20%, color-mix(in srgb,var(--accent) 38%,transparent) 0%, transparent 55%),
   linear-gradient(135deg,var(--brand),var(--accent));
 filter:hue-rotate(var(--hue,0deg))}
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
#cookie-notice[hidden]{display:none}
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
/* dashboard */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin:16px 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:14px 15px;box-shadow:var(--shadow)}
.kpi b{display:block;font-family:"Bricolage Grotesque",sans-serif;font-weight:700;font-size:25px;
 color:var(--brand-ink);line-height:1.15}
.kpi span{display:block;font-size:11.5px;color:var(--muted);margin-top:3px}
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
<link rel="alternate" type="application/rss+xml" title="sudtechjobs — dernières offres" href="{feed}">
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
  <a href="{dashboard}">Chiffres clés</a> ·
  job board tech du sud de la France
  <br><br>Une offre à ajouter, une remarque, ou juste envie de papoter du Sud&nbsp;?
  Écrivez-moi, ça fait toujours plaisir 🫰
  <a href="mailto:hello@sudtechjobs.com">✉️ hello@sudtechjobs.com</a>
  <br><br><a href="/a-propos.html">À propos</a> ·
  <a href="/mentions-legales.html">Mentions légales</a> ·
  <a href="/cgu.html">CGU</a> ·
  <a href="/confidentialite.html">Confidentialité</a>
  <div class="social">
    <a href="/feed.xml" title="Fil RSS des offres" aria-label="Fil RSS des offres">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.199 24C19.199 13.467 10.533 4.8 0 4.8V0c13.165 0 24 10.835 24 24h-4.801zM3.291 17.415a3.294 3.294 0 100 6.588 3.294 3.294 0 000-6.588zM15.909 24h-4.665c0-6.169-5.075-11.244-11.244-11.244V8.09c8.727 0 15.909 7.184 15.909 15.91z"/></svg>
    </a>
    <a href="https://www.linkedin.com/company/sudtechjobs/" target="_blank" rel="noopener" title="sudtechjobs sur LinkedIn" aria-label="sudtechjobs sur LinkedIn">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
    </a>
  </div>
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
        companies=SITE_URL + "/entreprise/", feed=SITE_URL + "/feed.xml",
        dashboard=SITE_URL + "/dashboard.html",
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
    # `datePosted` is REQUIRED. Prefer our own first-seen date over the source's
    # `published_at`: WTTJ frequently re-publishes with a months-old date, which
    # makes a live listing look stale and drags `validThrough` into the past, so
    # Google for Jobs drops it. Clamp so datePosted is never in the future.
    now_d = datetime.now(timezone.utc).date()

    def _as_date(s):
        try:
            return datetime.fromisoformat(
                (s or "").replace("Z", "+00:00")).date()
        except ValueError:
            return None

    dp_date = (_as_date(j.get("first_seen"))
               or _as_date(j.get("published_at")) or now_d)
    if dp_date > now_d:
        dp_date = now_d
    ld["datePosted"] = dp_date.isoformat()
    # A `validThrough` in the past signals a closed posting. This offer is still
    # in the feed, so keep the window open.
    vt_date = dp_date + timedelta(days=VALID_DAYS)
    if vt_date <= now_d:
        vt_date = now_d + timedelta(days=30)
    ld["validThrough"] = vt_date.isoformat()
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
def _job_meta_html(j):
    """meta pills shared by job_li and the company page's own job list:
    métier tag, "new" badge, remote, contract, experience, salary."""
    cat = j.get("category") or ""
    cat_label = CATS.get(cat, (cat,))[0] if cat else ""
    rem = j.get("remote_detail") or (
        j.get("remote") if j.get("remote") and "sur site" not in (j.get("remote") or "").lower() else "")
    if rem == "remote":
        rem = "full remote"
    if rem and "ponctuel" in rem.lower():
        rem = ""
    exp = j.get("experience") or ""
    exp_txt = ("%s d'expérience" % exp) if "ans" in exp else exp
    meta = []
    if cat_label:
        meta.append('<span class="tag %s">%s</span>' % (esc(cat), esc(cat_label)))
    if j.get("is_new"):
        meta.append('<span class="new">✨ nouveau</span>')
    if rem:
        meta.append('<span class="remote">%s</span>' % esc(rem))
    if j.get("contract"):
        meta.append("<span>%s</span>" % esc(j["contract"]))
    if exp_txt:
        meta.append("<span>%s</span>" % esc(exp_txt))
    if j.get("salary"):
        meta.append('<span class="sal">%s</span>' % esc(j["salary"]))
    return "".join(meta)


def _job_stack_html(j):
    stack = (j.get("stack") or [])[:10]
    if not stack:
        return ""
    return '<div class="stack">%s</div>' % "".join("<b>%s</b>" % esc(s) for s in stack)


def job_li(j):
    """rich job card for facet pages — same anatomy as the homepage's card:
    logo, title, company + city, stack chips, meta pills."""
    logo_html = _logo_html(j.get("logo"), j.get("company"), "logo")
    co_slug = j.get("_company_slug") or slugify(j.get("company"))
    city = j.get("city")
    city_html = (" · %s" % esc(city)) if city and city != "Remote" else ""
    return (
        '<li class="job"><div class="head">{logo}'
        '<div class="info"><div class="t"><a href="../offre/{slug}.html">{title}</a></div>'
        '<div class="co"><b><a href="../entreprise/{coslug}.html">{co}</a></b>{city}</div>'
        '</div></div>{stack}<div class="meta">{meta}</div></li>'
    ).format(
        logo=logo_html, slug=j["_slug"], title=esc(j.get("title")),
        coslug=esc(co_slug), co=esc(j.get("company")), city=city_html,
        stack=_job_stack_html(j), meta=_job_meta_html(j),
    )


# hash-based hue so repeat visits/pages give the same city the same tint
def _hue_seed(s):
    return int(hashlib.sha1((s or "").encode()).hexdigest(), 16) % 360


def _city_hero(city):
    buildings = [22, 40, 30, 54, 26, 46, 34, 20, 42, 28]
    x = 6
    bars = []
    for i, h in enumerate(buildings):
        w = 26
        bars.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2"></rect>'
                     % (x, 90 - h, w, h))
        x += w + 8
    return """<div class="city-hero" style="--hue:{hue}deg">
  <svg class="skyline" viewBox="0 0 {vw} 90" preserveAspectRatio="none" aria-hidden="true">{bars}</svg>
  <div class="city-hero-text">
    <span class="eyebrow">PACA · Sud de la France</span>
    <h1>Emplois tech à {city}</h1>
  </div>
</div>""".format(hue=_hue_seed(city), vw=x, bars="".join(bars), city=esc(city))


def render_facet(*, slug, h1, intro, jobs, siblings, generated, kind=None, city=None):
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
    header = (_city_hero(city) if (kind == "ville" and city) else "<h1>%s</h1>" % esc(h1))
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="{hub}">Emplois</a> › {h1}</nav>
{header}
<p class="sub">{intro}</p>
{facets}
<ul class="jobs">{lis}</ul>
""".format(home=SITE_URL + "/", hub=SITE_URL + "/emploi/", h1=esc(h1), header=header,
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
#  à propos                                                                   #
# --------------------------------------------------------------------------- #
ABOUT = """
<p class="sub">Le job board des métiers de la tech dans le sud de la France.</p>

<h2>Pourquoi ce site</h2>
<p>La tech dans le sud de la France, c'est Sophia-Antipolis, Marseille, Aix, Nice,
Montpellier, Toulon, des scale-ups, des ESN, des labos et les pôles French Tech
d'Aix-Marseille et de la Côte d'Azur. Mais quand on cherche un poste tech, tout
ramène à Paris. sudtechjobs rassemble au même endroit les offres tech, data,
produit et design de la région PACA (les autres régions du Sud suivront), mises à
jour tous les jours.</p>

<h2>Qui est derrière</h2>
<p>Anto. Je travaille dans la tech et j'adore le Sud. C'est un projet perso, fait
sur mon temps libre. Pas de société derrière, pas de levée, pas d'agenda caché :
juste l'envie d'un job board du Sud qui soit correct.</p>

<h2>D'où viennent les offres</h2>
<p>sudtechjobs est un agrégateur. Les annonces sont collectées automatiquement
depuis&nbsp;:</p>
<ul>
<li>Welcome to the Jungle (index public) ;</li>
<li>les outils de recrutement des entreprises en direct (Greenhouse, Lever, Ashby,
Teamtailor, Taleez, Recruitee et d'autres) ;</li>
<li>France Travail ;</li>
<li>les annuaires French Tech Aix-Marseille et Côte d'Azur, Telecom Valley,
Aktantis.</li>
</ul>
<p>Ce qui est fait dessus&nbsp;:</p>
<ul>
<li>un tri tech, data, produit, design par un classifieur maison (la catégorie
«&nbsp;métier&nbsp;» des sources est trop lacunaire pour s'y fier) ;</li>
<li>une déduplication quand la même offre apparaît sur plusieurs sources, en
gardant le lien vers le canal officiel ;</li>
<li>le retrait des offres expirées&nbsp;: la page devient un cul-de-sac, puis
disparaît ;</li>
<li>chaque offre renvoie vers l'annonce d'origine pour postuler. Je ne reçois
aucune candidature et aucun CV.</li>
</ul>
<p>Je ne suis affilié à aucune des entreprises listées. Une offre en trop, une
erreur, une demande de retrait&nbsp;? Écrivez à
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>, je corrige vite.</p>

<h2>Combien ça coûte</h2>
<p>Gratuit pour les candidats. Gratuit aussi pour les entreprises, pour l'instant.
Pas de compte à créer. La mesure d'audience se fait sans cookie ni donnée
personnelle (Umami). Voir la
<a href="/confidentialite.html">politique de confidentialité</a>.</p>

<h2>La suite</h2>
<p>Au programme&nbsp;: des alertes email par recherche enregistrée, plus de villes
et de régions du Sud. Un <a href="/guide-salaires-tech-paca.html">guide des
salaires tech en PACA</a> et un flux <a href="/feed.xml">RSS</a> sont déjà en
ligne. Une idée, une source à ajouter, ou
juste envie de papoter du Sud 🫰&nbsp;? Écrivez-moi, ça fait toujours plaisir&nbsp;:
<a href="mailto:hello@sudtechjobs.com">hello@sudtechjobs.com</a>
· <a href="https://www.linkedin.com/company/sudtechjobs/" target="_blank" rel="noopener">LinkedIn</a>.</p>
"""


def render_about():
    canonical = "%s/a-propos.html" % SITE_URL
    ld = {
        "@context": "https://schema.org",
        "@type": "AboutPage",
        "name": "À propos de sudtechjobs",
        "url": canonical,
        "publisher": {"@type": "Organization", "name": "sudtechjobs",
                      "url": SITE_URL + "/"},
    }
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › À propos</nav>
<h1>À propos de sudtechjobs</h1>
<div class="legal">
{inner}
</div>
""".format(home=SITE_URL + "/", inner=ABOUT)
    return shell(
        title="À propos | sudtechjobs",
        description="Qui est derrière sudtechjobs, pourquoi le site existe et d'où "
                    "viennent les offres d'emploi tech du sud de la France.",
        canonical=canonical, head_extra=jsonld(ld), body=body)


# --------------------------------------------------------------------------- #
#  guides (FAQ articles — /guide-....html, site root)                        #
# --------------------------------------------------------------------------- #
# City -> hiring zone, for the geographic breakdown. Only the cities that show
# up often enough in salary-disclosed postings to be worth a bucket; anything
# else is left out of that breakdown rather than mis-bucketed.
SALARY_ZONES = {
    "Marseille / Aix-en-Provence": (
        "Marseille", "Aix-en-Provence", "Vitrolles", "Marignane", "Aubagne",
        "La Ciotat", "Six-Fours-les-Plages", "La Seyne-sur-Mer"),
    "Nice / Sophia Antipolis": (
        "Sophia Antipolis", "Nice", "Valbonne", "Cagnes-sur-Mer", "Biot"),
    "Toulon / Var": ("Toulon", "Saint-Tropez"),
}
CITY_ZONE = {c: z for z, cities in SALARY_ZONES.items() for c in cities}

MIN_SAMPLE = 5  # below this, show the count but skip median/range as unreliable


def _parse_salary_eur(s):
    """'Annuel de 45000 Euros à 55000 Euros' / '45000–50000 EUR' -> (lo, hi) or None."""
    if not s:
        return None
    nums = [int(n) for n in re.findall(r"\d+", s.replace(" ", "").replace("\xa0", ""))]
    nums = [n for n in nums if n > 5000]  # drop stray small numbers (e.g. a duration)
    if not nums:
        return None
    return min(nums), max(nums)


def _fmt_keur(v):
    v = round(v / 500) * 500  # round to the nearest 500€, salaries are rarely finer
    return ("%.1f k€" % (v / 1000)) if v % 1000 else ("%d k€" % (v // 1000))


def compute_salary_stats(jobs):
    """Recomputed on every build from the live feed — the guide never goes stale."""
    parsed = []
    for j in jobs:
        p = _parse_salary_eur(j.get("salary"))
        if not p:
            continue
        lo, hi = p
        parsed.append({
            "mid": (lo + hi) / 2, "cat": j.get("category"),
            "exp": j.get("experience_min_years") or 0,
            "zone": CITY_ZONE.get(j.get("city")),
        })

    def stat(rows):
        n = len(rows)
        if n == 0:
            return {"n": 0}
        vals = sorted(r["mid"] for r in rows)
        return {"n": n, "median": statistics.median(vals),
                "lo": min(vals), "hi": max(vals)}

    by_cat = {}
    for cat in CATS:
        by_cat[cat] = stat([r for r in parsed if r["cat"] == cat])

    eng = [r for r in parsed if r["cat"] == "eng"]

    def exp_bucket(r):
        if r["exp"] < 2:
            return "junior"
        if r["exp"] < 5:
            return "confirme"
        return "senior"

    by_exp = {}
    for b in ("junior", "confirme", "senior"):
        by_exp[b] = stat([r for r in eng if exp_bucket(r) == b])

    by_zone = {}
    for zone in SALARY_ZONES:
        by_zone[zone] = stat([r for r in eng if r["zone"] == zone])

    return {
        "n_total": len(jobs), "n_salary": len(parsed),
        "overall": stat(parsed), "by_cat": by_cat,
        "by_exp": by_exp, "by_zone": by_zone,
    }


def _stat_line(label, s):
    if s["n"] < MIN_SAMPLE:
        return "<li><b>%s</b> — seulement %d offre%s avec salaire affiché, pas assez pour un chiffre fiable.</li>" % (
            esc(label), s["n"], "s" if s["n"] > 1 else "")
    return ("<li><b>%s</b> — médiane <b>%s</b> brut/an (%s offres, de %s à %s)</li>"
            % (esc(label), _fmt_keur(s["median"]), s["n"],
               _fmt_keur(s["lo"]), _fmt_keur(s["hi"])))


def render_salary_guide(jobs, generated):
    slug = "guide-salaires-tech-paca"
    canonical = "%s/%s.html" % (SITE_URL, slug)
    st = compute_salary_stats(jobs)
    ov = st["overall"]
    pct = round(100 * st["n_salary"] / st["n_total"]) if st["n_total"] else 0

    cat_list = "".join(
        _stat_line(CATS[cat][0], st["by_cat"][cat])
        for cat in ("eng", "data", "product", "design", "tech-adjacent")
        if st["by_cat"][cat]["n"] > 0)
    exp_list = "".join(
        _stat_line(label, st["by_exp"][key]) for key, label in (
            ("junior", "Junior (moins de 2 ans d’expérience)"),
            ("confirme", "Confirmé (2 à 5 ans)"),
            ("senior", "Senior (5 ans et plus)"),
        ))
    zone_list = "".join(_stat_line(zone, st["by_zone"][zone]) for zone in SALARY_ZONES)

    faq = [
        ("Quel est le salaire moyen dans la tech en PACA en 2026 ?",
         "<p>Sur les offres actuellement diffusées sur sudtechjobs qui affichent un "
         "salaire (%d offres sur %d, soit %d%% du flux), la médiane tous métiers "
         "confondus se situe autour de <b>%s brut par an</b>, avec un éventail qui va "
         "typiquement de %s à %s selon le métier, l’expérience et l’entreprise.</p>"
         % (ov["n"], st["n_total"], pct, _fmt_keur(ov["median"]) if ov["n"] else "n/a",
            _fmt_keur(ov["lo"]) if ov["n"] else "n/a", _fmt_keur(ov["hi"]) if ov["n"] else "n/a")),

        ("Le salaire change-t-il selon le métier (dev, data, produit, design) ?",
         "<p>Oui, et c’est souvent l’écart le plus net. D’après les offres avec salaire "
         "affiché en ce moment&nbsp;:</p><ul class=\"faq-stats\">%s</ul>"
         "<p>Le design et le produit ont trop peu d’offres avec salaire affiché pour un "
         "chiffre fiable : ces métiers sont sous-représentés dans l’agrégat par rapport "
         "au développement, pas forcément moins bien payés.</p>" % cat_list),

        ("Quel est l’écart de salaire entre un profil junior et un profil senior ?",
         "<p>Sur les postes de développement (l’échantillon le plus large)&nbsp;:</p>"
         "<ul class=\"faq-stats\">%s</ul>"
         "<p>L’écart type entre profils est réel mais souvent plus resserré qu’attendu sur "
         "la médiane : l’expérience élargit surtout le haut de fourchette (les postes "
         "seniors les mieux payés) plutôt qu’elle ne déplace la médiane. Beaucoup de grilles "
         "d’ESN et de PME du Sud restent proches d’une bande commune, indépendamment du "
         "niveau affiché.</p>" % exp_list),

        ("Marseille, Aix, Nice, Sophia Antipolis, Toulon : où les salaires "
         "tech sont-ils les plus élevés en PACA ?",
         "<p>Sur les postes de développement, avec assez de volume pour comparer&nbsp;:</p>"
         "<ul class=\"faq-stats\">%s</ul>"
         "<p>Les écarts entre bassins d’emploi de la région restent faibles : la vraie "
         "différence de salaire en PACA se joue sur le métier, le niveau d’expérience et "
         "le type d’entreprise (ESN, scale-up, grand groupe), pas sur la ville.</p>"
         % zone_list),

        ("Le salaire tech en PACA est-il plus bas qu’à Paris ?",
         "<p>En valeur affichée, souvent un peu, notamment sur les postes seniors et les "
         "profils rares (data/IA, plateformes). L’écart se réduit une fois pris en compte "
         "le coût de la vie et du logement, nettement plus élevé en Île-de-France, et il "
         "s’efface presque complètement pour les postes en full remote payés au même "
         "niveau national. C’est justement l’argument de beaucoup d’entreprises du Sud "
         "pour attirer des candidats parisiens : le salaire net d’un côté, le cadre de vie "
         "de l’autre.</p>"),

        ("Le télétravail change-t-il le salaire proposé ?",
         "<p>Pas de règle générale observée dans les offres du Sud : un poste en télétravail "
         "partiel (2–3 jours) proposé par une entreprise locale suit en général la même "
         "grille que ses postes sur site. C’est surtout le <i>type</i> d’entreprise qui fait "
         "varier le niveau — une scale-up ou une entreprise parisienne qui recrute en full "
         "remote depuis le Sud aligne parfois ses salaires sur sa propre grille nationale, "
         "au-dessus de la médiane régionale.</p>"),

        ("Pourquoi autant d’offres n’affichent pas de salaire ?",
         "<p>Seule environ une offre sur quatre (%d%% du flux actuel) précise une "
         "fourchette. C’est une habitude française plus qu’un signal en soi : beaucoup "
         "d’entreprises du Sud (ESN, PME, grands groupes) ne communiquent le montant qu’en "
         "entretien. Ne pas afficher de salaire ne veut pas dire qu’il est bas — mais ça "
         "vaut le coup de le demander tôt dans le process pour ne pas perdre de temps.</p>"
         % pct),

        ("Comment ces chiffres sont-ils calculés ?",
         "<p>Ce guide n’est pas une étude de marché figée : les chiffres ci-dessus sont "
         "recalculés à chaque mise à jour du site, directement à partir des %d offres tech, "
         "data, produit et design actuellement diffusées sur sudtechjobs en PACA, parmi "
         "lesquelles %d affichent une fourchette de salaire exploitable. Un chiffre reposant "
         "sur moins de %d offres n’est pas publié tel quel : le nombre d’offres est indiqué à "
         "chaque fois pour que vous puissiez juger vous-même de sa fiabilité.</p>"
         % (st["n_total"], st["n_salary"], MIN_SAMPLE)),

        ("Comment négocier son salaire avec ces chiffres en main ?",
         "<p>Trois réflexes simples&nbsp;: comparez-vous d’abord au même métier (dev, data, "
         "produit…), pas à la moyenne tous métiers confondus qui ne veut rien dire pour vous "
         "personnellement&nbsp;; regardez la fourchette haute de votre tranche d’expérience, "
         "pas seulement la médiane, si votre stack ou votre séniorité sort du lot&nbsp;; et "
         "demandez le budget dès le premier échange quand l’offre n’en affiche pas — ça évite "
         "d’avancer dans un process pour découvrir un écart trop grand à la fin.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide salaires tech PACA",
        h1="Quel salaire pour un poste tech en PACA en 2026&nbsp;? (dev, data, produit, design)",
        intro="Développeur, data/IA, produit, design&nbsp;: combien ça paie à Marseille, Aix, "
              "Nice, Sophia Antipolis ou Toulon&nbsp;? Ce guide répond aux questions les plus "
              "fréquentes à partir des offres réellement diffusées sur "
              "<a href=\"%s/\">sudtechjobs</a>, pas d’une étude de marché nationale hors-sol."
              % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Vous voulez comparer directement des offres&nbsp;?'
                   '<a href="/emploi/eng.html">Développement</a> · '
                   '<a href="/emploi/data.html">Data / IA</a> · '
                   '<a href="/emploi/product.html">Produit</a> · '
                   '<a href="/emploi/design.html">Design</a>.</p>',
        title="Salaire tech en PACA en 2026 : dev, data, produit, design | sudtechjobs",
        description="Combien gagne un développeur, un data/IA, un product manager ou un "
                    "designer en PACA en 2026 ? Chiffres calculés à partir des offres "
                    "réellement diffusées sur sudtechjobs, par métier, expérience et ville.")


def _render_faq_guide(*, slug, breadcrumb, h1, intro, faq, generated, links_html,
                       title, description):
    """Shared scaffold for a data-backed FAQ guide: Q&A body + FAQPage JSON-LD."""
    canonical = "%s/%s.html" % (SITE_URL, slug)
    faq_html = "".join(
        '<h2 id="q%d">%s</h2>%s' % (i, esc(q), a) for i, (q, a) in enumerate(faq, 1))

    ld = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{
            "@type": "Question", "name": q,
            "acceptedAnswer": {"@type": "Answer",
                               "answerText": re.sub(r"<[^>]+>", "", a).replace("&nbsp;", " ")},
        } for q, a in faq],
    }

    body = """
<nav class="bc"><a href="{home}">Accueil</a> › <a href="{home}guides/">Guides</a> › {breadcrumb}</nav>
<h1>{h1}</h1>
<div class="legal">
<p class="upd">Chiffres recalculés à chaque mise à jour du site — dernière génération&nbsp;: {gen}.</p>
<p class="sub">{intro}</p>
{faq}
{links}
<p class="sub"><a href="{home}guides/">← Tous les guides sudtechjobs</a></p>
</div>
""".format(home=SITE_URL + "/", breadcrumb=esc(breadcrumb), h1=h1, gen=esc(generated),
           intro=intro, faq=faq_html, links=links_html)

    card_title = h1.replace("&nbsp;", " ")
    html = shell(title=title, description=description, canonical=canonical,
                head_extra=jsonld(ld), body=body)
    return html, card_title, description


def _remote_bucket(j):
    r = j.get("remote") or j.get("remote_detail") or ""
    if r in REMOTE_FULL:
        return "remote"
    if r in ("hybride", "sur site", "ponctuel"):
        return r
    return None  # policy not disclosed


def compute_remote_stats(jobs):
    """Recomputed on every build, like compute_salary_stats — never goes stale."""
    def stat(rows):
        n = len(rows)
        known = [r for r in rows if r["bucket"]]
        n_known = len(known)
        if n_known == 0:
            return {"n": n, "n_known": 0}
        some_remote = sum(1 for r in known if r["bucket"] != "sur site")
        return {"n": n, "n_known": n_known,
                "pct_remote": round(100 * some_remote / n_known),
                "pct_full": round(100 * sum(1 for r in known if r["bucket"] == "remote") / n_known)}

    rows = [{"cat": j.get("category"), "bucket": _remote_bucket(j)} for j in jobs]

    by_cat = {cat: stat([r for r in rows if r["cat"] == cat]) for cat in CATS}

    top_remote = Counter()
    top_hybride = Counter()
    for j in jobs:
        b = _remote_bucket(j)
        if b == "remote":
            top_remote[j.get("company")] += 1
        elif b == "hybride":
            top_hybride[j.get("company")] += 1

    return {
        "overall": stat(rows), "by_cat": by_cat,
        "top_remote": [(c, n) for c, n in top_remote.most_common(6) if n >= 2],
        "top_hybride": [(c, n) for c, n in top_hybride.most_common(6) if n >= 3],
    }


def _remote_pct_line(label, s):
    if s.get("n_known", 0) < MIN_SAMPLE:
        return "<li><b>%s</b> — trop peu d’offres précisant leur politique de télétravail " \
               "pour un chiffre fiable.</li>" % esc(label)
    return ("<li><b>%s</b> — <b>%d%%</b> proposent au moins du télétravail ponctuel "
            "(dont %d%% en full remote), sur %d offres qui précisent leur politique.</li>"
            % (esc(label), s["pct_remote"], s["pct_full"], s["n_known"]))


def render_remote_guide(jobs, generated):
    slug = "guide-teletravail-tech-paca"
    st = compute_remote_stats(jobs)
    ov = st["overall"]
    pct_disclosed = round(100 * ov["n_known"] / ov["n"]) if ov["n"] else 0

    cat_list = "".join(
        _remote_pct_line(CATS[cat][0], st["by_cat"][cat])
        for cat in ("eng", "data", "product", "design", "tech-adjacent")
        if st["by_cat"][cat]["n"] > 0)

    def company_list(rows, word):
        if not rows:
            return "pas assez d’offres pour dégager des entreprises récurrentes en ce moment"
        return ", ".join("<b>%s</b> (%d offre%s %s)" % (esc(c), n, "s" if n > 1 else "", word)
                          for c, n in rows)

    faq = [
        ("Quelle part des offres tech en PACA propose du télétravail ?",
         "<p>Sur les offres tech, data, produit et design actuellement diffusées sur "
         "sudtechjobs, %d%% précisent une politique de télétravail (le reste ne dit rien, "
         "voir plus bas). Parmi celles qui le précisent, <b>%d%%</b> proposent au moins du "
         "télétravail ponctuel — hybride, ponctuel ou full remote — et <b>%d%%</b> sont "
         "en full remote.</p>" % (pct_disclosed, ov.get("pct_remote", 0), ov.get("pct_full", 0))),

        ("Full remote, hybride, ponctuel : quelle est la différence, et qu’est-ce qui domine ?",
         "<p><b>Full remote</b>&nbsp;: le poste se fait entièrement à distance, aucun jour "
         "sur site imposé. <b>Hybride</b>&nbsp;: un rythme fixe de jours au bureau (souvent "
         "2 à 4 jours/semaine dans les offres du Sud). <b>Ponctuel</b>&nbsp;: télétravail "
         "possible occasionnellement, sans rythme fixe, à la discrétion du manager. En PACA, "
         "c’est <b>l’hybride qui domine largement</b> les offres qui précisent une politique "
         "— le full remote reste minoritaire, contrairement à ce qu’on voit parfois sur des "
         "boards nationaux dominés par des postes 100% à distance.</p>"),

        ("Le télétravail est-il plus fréquent pour un poste dev, data ou produit ?",
         "<p>Oui, nettement. D’après les offres qui précisent leur politique&nbsp;:</p>"
         "<ul class=\"faq-stats\">%s</ul>"
         "<p>Les postes produit et data ont proportionnellement plus de télétravail que le "
         "développement pur — une partie du développement en PACA passe par des ESN qui "
         "placent leurs consultants chez le client, ce qui pousse vers l’hybride ou le sur "
         "site plutôt que le full remote.</p>" % cat_list),

        ("Quelles entreprises proposent du full remote en PACA ?",
         "<p>Parmi les entreprises qui recrutent actuellement en full remote sur "
         "sudtechjobs&nbsp;: %s. Ce ne sont que les entreprises les plus présentes dans le "
         "flux du moment, pas une liste exhaustive ni figée — elle change avec les offres "
         "publiées.</p>" % company_list(st["top_remote"], "en full remote")),

        ("Quelles entreprises proposent le plus d’hybride, et à quel rythme ?",
         "<p>Sur le même principe, les entreprises les plus présentes en hybride en ce "
         "moment&nbsp;: %s. Le rythme exact (2, 3 ou 4 jours de télétravail) n’est presque "
         "jamais le même d’une entreprise à l’autre — il se négocie souvent en entretien "
         "plutôt qu’il n’est figé dans l’annonce, à demander explicitement si l’offre reste "
         "vague.</p>" % company_list(st["top_hybride"], "en hybride")),

        ("Pourquoi tant d’offres ne précisent pas leur politique de télétravail ?",
         "<p>Environ %d%% du flux actuel ne mentionne rien. C’est plus transparent que pour "
         "le salaire (où seule une offre sur quatre communique un montant), mais ça reste "
         "un angle mort fréquent&nbsp;: beaucoup de PME et d’ESN du Sud gèrent le télétravail "
         "au cas par cas plutôt que comme une politique affichée. Ne pas voir de mention ne "
         "veut pas dire « zéro télétravail » — ça vaut le coup de demander en entretien.</p>"
         % (100 - pct_disclosed)),

        ("Un poste « télétravail » en PACA est-il vraiment basé dans le Sud ?",
         "<p>Pas toujours. Une partie des offres en full remote proviennent d’entreprises "
         "dont le siège est ailleurs (souvent Paris) et qui recrutent à distance sans "
         "exiger de présence dans le Sud&nbsp;: le poste est ouvert aux candidats basés en "
         "PACA, mais l’équipe et les rares journées sur site, s’il y en a, seront ailleurs. "
         "Pour un poste réellement ancré dans l’écosystème local (rencontrer l’équipe, les "
         "meetups, les bureaux du Sud), l’hybride avec une ville PACA précisée est un "
         "signal plus fiable que « full remote » seul.</p>"),

        ("Comment filtrer uniquement les offres télétravail sur sudtechjobs ?",
         "<p>Le plus simple&nbsp;: la page <a href=\"/emploi/teletravail.html\">Emplois en "
         "télétravail</a>, ou le filtre télétravail directement sur la <a href=\"/\">page "
         "d’accueil</a>, à combiner avec un filtre métier ou ville pour restreindre encore "
         "plus.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide télétravail tech PACA",
        h1="Télétravail dans la tech en PACA en 2026&nbsp;: quelles entreprises, quel rythme&nbsp;?",
        intro="Full remote, hybride, ponctuel&nbsp;: quelle part des offres tech du Sud propose "
              "vraiment du télétravail, et quelles entreprises recrutent en ce moment sans "
              "exiger d’être sur site&nbsp;? Réponses à partir des offres réellement diffusées "
              "sur <a href=\"%s/\">sudtechjobs</a>." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/emploi/teletravail.html">Télétravail</a>.</p>',
        title="Télétravail tech en PACA en 2026 : quelles entreprises, quel rythme | sudtechjobs",
        description="Full remote, hybride, ponctuel : quelle part des offres tech en PACA "
                    "propose du télétravail, pour quels métiers, et quelles entreprises "
                    "recrutent en ce moment sans exiger d'être sur site ?")


def compute_hiring_stats(jobs):
    """Recomputed on every build, like the other guides — a live snapshot, not a ranking."""
    counts = Counter()
    slug_by_company = {}
    for j in jobs:
        c = j.get("company")
        counts[c] += 1
        if j.get("_company_slug"):
            slug_by_company[c] = j["_company_slug"]

    by_cat = {cat: Counter() for cat in CATS}
    for j in jobs:
        cat = j.get("category")
        if cat in by_cat:
            by_cat[cat][j.get("company")] += 1

    by_zone = {zone: Counter() for zone in SALARY_ZONES}
    for j in jobs:
        z = CITY_ZONE.get(j.get("city"))
        if z:
            by_zone[z][j.get("company")] += 1

    return {
        "n_total": len(jobs), "n_companies": len(counts),
        "overall_top": counts.most_common(10),
        "by_cat_top": {cat: by_cat[cat].most_common(5) for cat in CATS},
        "by_zone_top": {zone: by_zone[zone].most_common(3) for zone in SALARY_ZONES},
        "slug_by_company": slug_by_company,
    }


def _company_link(name, slugs):
    slug = slugs.get(name)
    if slug:
        return '<a href="/entreprise/%s.html">%s</a>' % (esc(slug), esc(name))
    return "<b>%s</b>" % esc(name)


def render_hiring_guide(jobs, generated):
    slug = "guide-entreprises-qui-recrutent-tech-paca"
    st = compute_hiring_stats(jobs)
    slugs = st["slug_by_company"]

    def name_list(rows, with_n=True):
        if not rows:
            return "pas assez d’offres pour dégager une tendance nette en ce moment"
        return ", ".join(
            "%s%s" % (_company_link(c, slugs),
                      " (%d offre%s)" % (n, "s" if n > 1 else "") if with_n else "")
            for c, n in rows)

    top10_html = "<ol style=\"margin:6px 0 13px;padding-left:20px\">%s</ol>" % "".join(
        "<li>%s — %d offre%s ouvertes</li>" % (_company_link(c, slugs), n, "s" if n > 1 else "")
        for c, n in st["overall_top"])

    cat_list = "".join(
        "<li><b>%s</b> — %s</li>" % (esc(CATS[cat][0]), name_list(st["by_cat_top"][cat]))
        for cat in ("eng", "data", "product", "design", "tech-adjacent")
        if st["by_cat_top"][cat])

    zone_list = "".join(
        "<li><b>%s</b> — %s</li>" % (esc(zone), name_list(st["by_zone_top"][zone]))
        for zone in SALARY_ZONES if st["by_zone_top"][zone])

    faq = [
        ("Quelles entreprises tech recrutent le plus en PACA en ce moment ?",
         "<p>Sur les %d offres tech, data, produit et design actuellement diffusées sur "
         "sudtechjobs (réparties sur %d entreprises), les 10 employeurs avec le plus "
         "d’offres ouvertes en ce moment&nbsp;:</p>%s"
         "<p>C’est un volume d’offres publiées, pas un classement qualité employeur ni une "
         "recommandation&nbsp;: une grosse entreprise avec beaucoup de turnover peut publier "
         "plus d’offres qu’une petite boîte qui recrute rarement mais dans de bonnes "
         "conditions.</p>" % (st["n_total"], st["n_companies"], top10_html)),

        ("Ce classement est-il figé ?",
         "<p>Non, il est recalculé à chaque mise à jour du site à partir des offres "
         "réellement en ligne&nbsp;: une entreprise qui pourvoit ses postes ou arrête de "
         "recruter en sort, une autre qui ouvre une campagne de recrutement y entre. Ce "
         "n’est pas une liste d’entreprises figée à surveiller une fois pour toutes.</p>"),

        ("Qui recrute le plus en développement, data ou produit ?",
         "<p>Le classement change sensiblement selon le métier&nbsp;:</p><ul>%s</ul>"
         "<p>Les gros volumes en développement viennent surtout de l’industrie (défense, "
         "naval) et des ESN&nbsp;; la data et le produit sont davantage portés par des "
         "éditeurs et des scale-ups.</p>" % cat_list),

        ("Marseille, Aix, Sophia Antipolis, Toulon : les mêmes entreprises "
         "recrutent-elles partout ?",
         "<p>Non, chaque bassin d’emploi a sa propre dominante&nbsp;:</p><ul>%s</ul>"
         "<p>C’est souvent plus révélateur que le classement global&nbsp;: une entreprise "
         "très présente dans le top 10 national du Sud peut être quasi absente d’une ville "
         "donnée, et inversement.</p>" % zone_list),

        ("Pourquoi des groupes industriels comme Thales ou Naval Group apparaissent "
         "dans une recherche « tech » ?",
         "<p>Parce qu’ils en sont, dans le Sud plus qu’ailleurs&nbsp;: Sophia Antipolis et "
         "le bassin toulonnais concentrent une grosse activité d’ingénierie logicielle "
         "embarquée, systèmes et cybersécurité pour la défense et le naval — des postes de "
         "développeur, d’ingénieur systèmes ou de data au même titre qu’en startup, "
         "simplement chez un industriel plutôt qu’un éditeur. Le classement les inclut "
         "parce que le poste est réellement technique, pas parce que l’entreprise est "
         "labellisée « tech ».</p>"),

        ("ESN vs entreprises qui recrutent en direct : comment les distinguer "
         "dans cette liste ?",
         "<p>Une bonne partie du volume vient de sociétés de conseil informatique (ESN) qui "
         "recrutent pour ensuite placer le profil chez un client — Sopra Steria, Capgemini, "
         "CGI, Groupe SII, Atos, Keyrus ou eXalt en sont des exemples connus. En face, des "
         "entreprises comme Alan, Thales ou Naval Group recrutent en direct&nbsp;: vous "
         "travaillez pour elles, pas pour un client qu’elles vous affectent. Ni l’un ni "
         "l’autre n’est un meilleur choix dans l’absolu&nbsp;: l’ESN donne de la variété de "
         "missions et souvent plus de flexibilité géographique, le direct donne plus de "
         "visibilité sur le produit final et l’équipe. À vérifier en entretien si l’intitulé "
         "de poste ne le précise pas.</p>"),

        ("Comment être alerté quand une de ces entreprises publie une nouvelle offre ?",
         "<p>La fiche de chaque entreprise (accessible depuis <a href=\"/entreprise/\">la "
         "liste des entreprises</a>) liste ses offres du moment. Pour ne rien rater "
         "automatiquement&nbsp;: une alerte email sur une recherche enregistrée (bouton "
         "« recevoir ces offres par email » sur la page d’accueil), ou le "
         "<a href=\"/feed.xml\">flux RSS</a> de sudtechjobs.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide entreprises qui recrutent",
        h1="Quelles entreprises tech recrutent le plus en PACA en 2026&nbsp;?",
        intro="Thales, Naval Group, Alan, Sopra Steria, Capgemini&nbsp;: qui recrute vraiment "
              "dans la tech en PACA en ce moment, pour quels métiers et dans quelle ville&nbsp;? "
              "Classement calculé à partir des offres réellement diffusées sur "
              "<a href=\"%s/\">sudtechjobs</a>, pas d’un baromètre marque employeur." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/entreprise/">Toutes les entreprises</a>.</p>',
        title="Quelles entreprises tech recrutent le plus en PACA en 2026 | sudtechjobs",
        description="Thales, Naval Group, Alan, Sopra Steria, Capgemini : classement des "
                    "entreprises qui recrutent le plus dans la tech en PACA en ce moment, "
                    "par métier et par ville, calculé à partir des offres réelles.")


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_dashboard_stats(jobs):
    """Recomputed on every build, like the guides — a live snapshot of the feed."""
    now = datetime.now(timezone.utc)
    new_7d = new_30d = 0
    for j in jobs:
        dt = _parse_iso(j.get("first_seen"))
        if not dt:
            continue
        age = (now - dt).days
        if age <= 7:
            new_7d += 1
        if age <= 30:
            new_30d += 1

    dept_counts = Counter()
    remote_n = 0
    for j in jobs:
        city = j.get("city") or ""
        if city == "Remote" or (j.get("remote") or "") in REMOTE_FULL:
            remote_n += 1
        code = CITY_DEPT.get(slugify(city))
        if code:
            dept_counts[code] += 1

    cat_counts = Counter(j.get("category") for j in jobs if j.get("category") in CATS)

    return {
        "n_total": len(jobs), "new_7d": new_7d, "new_30d": new_30d,
        "remote_n": remote_n,
        "dept_counts": dept_counts, "cat_counts": cat_counts,
        "hiring": compute_hiring_stats(jobs),
        "remote": compute_remote_stats(jobs),
        "salary": compute_salary_stats(jobs),
    }


def render_dashboard(jobs, generated):
    slug = "dashboard"
    canonical = "%s/%s.html" % (SITE_URL, slug)
    st = compute_dashboard_stats(jobs)
    slugs = st["hiring"]["slug_by_company"]
    n_total = st["n_total"]

    def kpi(value, label):
        return '<div class="kpi"><b>%s</b><span>%s</span></div>' % (esc(value), esc(label))

    kpis = [kpi(n_total, "offres ouvertes en ce moment"),
            kpi(st["hiring"]["n_companies"], "entreprises qui recrutent"),
            kpi(st["new_7d"], "offres apparues cette semaine")]
    rem = st["remote"]["overall"]
    if rem.get("n_known", 0) >= MIN_SAMPLE:
        kpis.append(kpi("%d%%" % rem["pct_remote"], "au moins du télétravail"))
    sal_eng = st["salary"]["by_cat"].get("eng", {"n": 0})
    if sal_eng.get("n", 0) >= MIN_SAMPLE:
        kpis.append(kpi(_fmt_keur(sal_eng["median"]), "salaire médian dev, brut/an"))
    kpi_html = '<div class="kpi-grid">%s</div>' % "".join(kpis)

    cat_items = "".join(
        "<li><b>%s</b> — %d offre%s</li>" % (esc(CATS[cat][0]), n, "s" if n > 1 else "")
        for cat, n in st["cat_counts"].most_common())
    dept_items = "".join(
        "<li><b>%s</b> — %d offre%s</li>" % (esc(PACA_DEPTS[code][1]), n, "s" if n > 1 else "")
        for code, n in st["dept_counts"].most_common())
    if st["remote_n"]:
        dept_items += "<li><b>Télétravail</b> — %d offre%s</li>" % (
            st["remote_n"], "s" if st["remote_n"] > 1 else "")

    top10 = st["hiring"]["overall_top"]
    top_html = ("<ol style=\"margin:6px 0 13px;padding-left:20px\">%s</ol>" % "".join(
        "<li>%s — %d offre%s ouvertes</li>" % (_company_link(c, slugs), n, "s" if n > 1 else "")
        for c, n in top10)) if top10 else "<p class=\"sub\">Pas assez de données pour un classement.</p>"

    ld = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Offres d'emploi tech en PACA — sudtechjobs",
        "description": "Instantané du marché de l'emploi tech (dev, data, produit, design) "
                        "en Provence-Alpes-Côte d'Azur, recalculé à chaque mise à jour du site "
                        "à partir des offres réellement en ligne.",
        "url": canonical,
        "creator": {"@type": "Organization", "name": "sudtechjobs", "url": SITE_URL + "/"},
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": SITE_URL + "/jobs.json"},
            {"@type": "DataDownload", "encodingFormat": "application/rss+xml",
             "contentUrl": SITE_URL + "/feed.xml"},
        ],
    }

    body = """
<nav class="bc"><a href="{home}">Accueil</a> › Chiffres clés</nav>
<h1>Le marché de l’emploi tech en PACA, en chiffres</h1>
<div class="legal">
<p class="upd">Recalculé à chaque mise à jour du site à partir des offres réellement en ligne — dernière génération&nbsp;: {gen}.</p>
<p class="sub">Un instantané du recrutement tech (dev, data, produit, design) en Provence-Alpes-Côte d’Azur, calculé à partir des {n} offres actuellement diffusées sur <a href="{home}">sudtechjobs</a> — pas un baromètre déclaratif.</p>
{kpis}
<h2>Par métier</h2>
<ul>{cats}</ul>
<h2>Par territoire</h2>
<ul>{depts}</ul>
<h2>Qui recrute le plus en ce moment</h2>
{top}
<p class="note">Un volume d’offres publiées, pas un classement qualité employeur&nbsp;: une entreprise avec du turnover peut publier plus d’offres qu’une petite structure qui recrute rarement. Détail par entreprise sur <a href="{companies}">la liste des entreprises</a>.</p>
<h2>Données ouvertes</h2>
<p>Ces chiffres sont recalculés depuis le flux public&nbsp;: <a href="{home}jobs.json">jobs.json</a> (offres actives), <a href="{home}feed.xml">feed.xml</a> (RSS), <a href="{home}sitemap.xml">sitemap.xml</a>. Voir aussi <a href="{home}guides/">les guides</a> (salaires, télétravail, entreprises qui recrutent) pour l’analyse détaillée.</p>
</div>
""".format(home=SITE_URL + "/", gen=esc(generated), n=n_total, kpis=kpi_html,
           cats=cat_items, depts=dept_items, top=top_html,
           companies=SITE_URL + "/entreprise/")

    return shell(
        title="Chiffres clés du recrutement tech en PACA | sudtechjobs",
        description="Offres ouvertes, entreprises qui recrutent, part de télétravail et "
                    "salaire médian dev : le marché de l'emploi tech en Provence-Alpes-Côte "
                    "d'Azur en chiffres, recalculé en direct depuis sudtechjobs.",
        canonical=canonical, head_extra=jsonld(ld), body=body)


def render_guides_hub(guides, generated):
    """guides: [(slug, title, description), ...] in display order."""
    cards = "".join(
        '<li><a href="/%s.html"><span class="t">%s</span>'
        '<span class="co">%s</span></a></li>' % (esc(slug), esc(title), esc(desc))
        for slug, title, desc in guides)
    body = """
<nav class="bc"><a href="{home}">Accueil</a> › Guides</nav>
<h1>Guides sudtechjobs</h1>
<p class="sub">Des réponses aux questions les plus fréquentes sur la tech en PACA, calculées
à partir des offres réellement diffusées sur sudtechjobs — jamais figées, recalculées à
chaque mise à jour du site.</p>
<ul class="jobs">{cards}</ul>
""".format(home=SITE_URL + "/", cards=cards)
    return shell(
        title="Guides sudtechjobs : salaires, télétravail, recrutement tech en PACA",
        description="Tous les guides sudtechjobs sur la tech en PACA : salaires, "
                    "télétravail, entreprises qui recrutent, et plus à venir.",
        canonical="%s/guides/" % SITE_URL, body=body)


def compute_intern_stats(jobs):
    """Recomputed on every build, like the other guides."""
    rows = [j for j in jobs if j.get("contract") in ("Stage", "Alternance")]

    by_contract = Counter(j.get("contract") for j in rows)
    by_cat = Counter(j.get("category") for j in rows)
    by_company = Counter(j.get("company") for j in rows)
    by_zone = Counter()
    for j in rows:
        z = CITY_ZONE.get(j.get("city"))
        if z:
            by_zone[z] += 1

    return {
        "n_total": len(jobs), "n": len(rows),
        "by_contract": by_contract,
        "by_cat_top": [(cat, n) for cat, n in by_cat.most_common() if n > 0],
        "top_companies": [(c, n) for c, n in by_company.most_common(6) if n >= 2],
        "by_zone": [(z, by_zone.get(z, 0)) for z in SALARY_ZONES if by_zone.get(z, 0) > 0],
    }


def render_intern_guide(jobs, generated):
    slug = "guide-stage-alternance-tech-paca"
    st = compute_intern_stats(jobs)
    pct = round(100 * st["n"] / st["n_total"]) if st["n_total"] else 0

    cat_list = "".join(
        "<li><b>%s</b> — %d offre%s</li>" % (esc(CATS.get(cat, (cat,))[0]), n, "s" if n > 1 else "")
        for cat, n in st["by_cat_top"])

    if st["top_companies"]:
        company_txt = ", ".join(
            "<b>%s</b> (%d)" % (esc(c), n) for c, n in st["top_companies"])
    else:
        company_txt = "pas assez d’offres pour dégager des entreprises récurrentes en ce moment"

    if st["by_zone"]:
        zone_txt = ", ".join("<b>%s</b> (%d)" % (esc(z), n) for z, n in st["by_zone"])
    else:
        zone_txt = "pas assez d’offres pour comparer les bassins d’emploi en ce moment"

    faq = [
        ("Combien y a-t-il d’offres de stage et d’alternance tech en PACA en ce moment ?",
         "<p>%d offres sur les %d actuellement diffusées sur sudtechjobs (%d%% du flux), "
         "dont %d stages et %d alternances. C’est un volume réel mais modeste&nbsp;: la "
         "tech en PACA reste un marché où l’essentiel des offres est en CDI direct.</p>"
         % (st["n"], st["n_total"], pct,
            st["by_contract"].get("Stage", 0), st["by_contract"].get("Alternance", 0))),

        ("Stage ou alternance : quelle est la différence ?",
         "<p>Le <b>stage</b> est une période temporaire (quelques mois) intégrée à une "
         "formation, sans contrat de travail classique, avec une gratification (pas un "
         "vrai salaire). L’<b>alternance</b> (apprentissage ou professionnalisation) est un "
         "vrai contrat de travail à temps partagé entre l’entreprise et l’école, rémunéré "
         "selon une grille légale liée à l’âge et au niveau d’études, et débouche plus "
         "souvent sur une embauche directe à la clé.</p>"),

        ("Quelles entreprises recrutent le plus en stage/alternance en ce moment ?",
         "<p>%s. Ce sont surtout des grands groupes et ESN (Capgemini, Sopra Steria, "
         "Wavestone, Deloitte…) qui structurent des campagnes de recrutement stage/"
         "alternance chaque année&nbsp;: une bonne partie du volume vient d’elles plutôt "
         "que de petites startups qui recrutent au coup par coup.</p>" % company_txt),

        ("Dans quelles villes trouve-t-on le plus de stages et d’alternances ?",
         "<p>%s. Sans surprise, ce sont les bassins qui concentrent aussi le plus gros "
         "volume d’offres tech tout court (voir le <a href=\"/guide-entreprises-qui-"
         "recrutent-tech-paca.html\">guide des entreprises qui recrutent</a>).</p>"
         % zone_txt),

        ("Pour quels métiers trouve-t-on des stages et alternances ?",
         "<p>La répartition par métier&nbsp;:</p><ul>%s</ul>"
         "<p>Le développement domine très largement&nbsp;: c’est le métier qui a le plus "
         "besoin de volume junior à former en continu.</p>" % cat_list),

        ("Ce volume est-il représentatif de tout ce qui existe réellement ?",
         "<p>Non, probablement pas complètement. Une bonne partie des stages et "
         "alternances tech se pourvoit via les réseaux d’écoles (42 Nice, Epitech, Ynov, "
         "Simplon, Polytech…), les forums étudiants et le bouche-à-oreille, sans jamais "
         "passer par Welcome to the Jungle ni par les pages carrière publiques que "
         "sudtechjobs agrège. Le nombre réel d’opportunités est très certainement plus "
         "élevé que ce que montre ce guide — considérez-le comme un aperçu du marché "
         "« public », pas comme l’inventaire complet.</p>"),

        ("Comment repérer une offre sérieuse et bien préparer sa candidature ?",
         "<p>Quelques signaux utiles&nbsp;: une fiche de poste précise (missions, stack, "
         "durée, gratification/rémunération annoncée) plutôt qu’une annonce vague et "
         "recyclée d’année en année&nbsp;; un tuteur ou maître d’apprentissage identifié "
         "dès l’entretien&nbsp;; et, pour l’alternance, la confirmation que l’entreprise a "
         "déjà accueilli des alternants (le rythme école/entreprise, souvent mal expliqué "
         "en entretien, vaut la peine d’être posé comme question directe).</p>"),

        ("Comment être alerté sur les nouvelles offres de stage et d’alternance ?",
         "<p>Sur la <a href=\"/\">page d’accueil</a>, le filtre « Tous contrats » permet de "
         "restreindre l’affichage au stage ou à l’alternance, à combiner avec une ville ou "
         "un métier&nbsp;; une alerte email peut ensuite être créée sur cette recherche "
         "précise pour être prévenu à chaque nouvelle offre.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide stage & alternance",
        h1="Stage et alternance tech en PACA en 2026&nbsp;: où et comment postuler&nbsp;?",
        intro="Développement, data, design&nbsp;: combien de stages et d’alternances tech "
              "sont réellement ouverts en PACA en ce moment, chez qui, et dans quelle "
              "ville&nbsp;? Chiffres calculés à partir des offres diffusées sur "
              "<a href=\"%s/\">sudtechjobs</a>." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/">Page d’accueil (filtre « Tous contrats »)</a>.</p>',
        title="Stage et alternance tech en PACA en 2026 : où et comment postuler | sudtechjobs",
        description="Combien de stages et d'alternances tech sont ouverts en PACA en ce "
                    "moment, chez quelles entreprises et dans quelle ville, calculé à "
                    "partir des offres réellement diffusées sur sudtechjobs.")


def compute_junior_stats(jobs):
    """Recomputed on every build. Only experience_min_years == 0 counts as
    'débutant accepté' — a None value means undisclosed, not junior-friendly."""
    junior = [j for j in jobs if j.get("experience_min_years") == 0]

    by_cat = Counter(j.get("category") for j in junior)
    by_company = Counter(j.get("company") for j in junior)
    by_zone = Counter()
    for j in junior:
        z = CITY_ZONE.get(j.get("city"))
        if z:
            by_zone[z] += 1
    n_bac5 = sum(1 for j in junior if j.get("education_level") == "bac_5")
    n_edu_known = sum(1 for j in junior if j.get("education_level"))

    return {
        "n_total": len(jobs), "n": len(junior),
        "n_undisclosed": sum(1 for j in jobs if j.get("experience_min_years") is None),
        "by_cat_top": [(cat, n) for cat, n in by_cat.most_common() if n > 0],
        "top_companies": [(c, n) for c, n in by_company.most_common(6) if n >= 2],
        "by_zone": [(z, by_zone.get(z, 0)) for z in SALARY_ZONES if by_zone.get(z, 0) > 0],
        "n_bac5": n_bac5, "n_edu_known": n_edu_known,
    }


def render_junior_guide(jobs, generated):
    slug = "guide-premier-emploi-junior-tech-paca"
    st = compute_junior_stats(jobs)
    pct = round(100 * st["n"] / st["n_total"]) if st["n_total"] else 0
    pct_undisclosed = round(100 * st["n_undisclosed"] / st["n_total"]) if st["n_total"] else 0

    cat_list = "".join(
        "<li><b>%s</b> — %d offre%s</li>" % (esc(CATS.get(cat, (cat,))[0]), n, "s" if n > 1 else "")
        for cat, n in st["by_cat_top"])

    company_txt = (", ".join("<b>%s</b> (%d)" % (esc(c), n) for c, n in st["top_companies"])
                   if st["top_companies"]
                   else "pas assez d’offres pour dégager des entreprises récurrentes en ce moment")
    zone_txt = (", ".join("<b>%s</b> (%d)" % (esc(z), n) for z, n in st["by_zone"])
                if st["by_zone"]
                else "pas assez d’offres pour comparer les bassins d’emploi en ce moment")
    pct_bac5 = round(100 * st["n_bac5"] / st["n_edu_known"]) if st["n_edu_known"] else None

    faq = [
        ("Combien d’offres tech en PACA sont vraiment accessibles sans expérience ?",
         "<p>%d offres sur les %d actuellement diffusées sur sudtechjobs affichent "
         "explicitement « débutant accepté, 0 an d’expérience » (%d%% du flux). Ce n’est "
         "qu’un plancher&nbsp;: %d%% des offres du site ne précisent aucune expérience "
         "minimale — elles ne sont pas forcément fermées aux débutants, l’information "
         "manque simplement.</p>" % (st["n"], st["n_total"], pct, pct_undisclosed)),

        ("Sur quels métiers ces offres débutant sont-elles les plus fréquentes ?",
         "<p>La répartition par métier parmi les offres « 0 an d’expérience »&nbsp;:</p>"
         "<ul>%s</ul>"
         "<p>Le développement concentre l’essentiel du volume débutant, comme pour le "
         "reste du marché&nbsp;: voir aussi le <a href=\"/guide-stage-alternance-tech-"
         "paca.html\">guide stage &amp; alternance</a> pour les profils encore en "
         "formation.</p>" % cat_list),

        ("Quelles entreprises recrutent le plus de profils débutants en ce moment ?",
         "<p>%s. Comme pour le stage et l’alternance, ce sont surtout des ESN et grands "
         "groupes qui structurent un volume régulier de recrutement junior, plutôt que des "
         "petites structures qui recrutent au coup par coup.</p>" % company_txt),

        ("Faut-il un bac+5 pour décrocher un premier poste tech, même sans "
         "expérience exigée ?",
         "<p>%s Beaucoup d’offres « débutant accepté » ne précisent pas de niveau "
         "d’études du tout — l’absence de bac+5 affiché n’est pas un rejet, mais un "
         "diplôme d’ingénieur reste, dans les faits, le profil le plus visible sur cette "
         "partie du marché.</p>"
         % ("Pas systématiquement, mais c’est fréquent&nbsp;: parmi les offres débutant "
            "qui précisent un niveau d’études, %d%% demandent un bac+5." % pct_bac5
            if pct_bac5 is not None else
            "Les données actuelles ne permettent pas de trancher avec assez de recul.")),

        ("Où se trouvent le plus d’offres débutant en PACA ?",
         "<p>%s. Même logique que pour le reste du marché tech régional&nbsp;: le volume "
         "suit la taille du bassin d’emploi plus qu’une politique « junior friendly » "
         "propre à telle ou telle ville.</p>" % zone_txt),

        ("Et si aucune offre « débutant » ne correspond, que faire ?",
         "<p>Élargir la recherche à ce qui n’affiche <i>aucune</i> expérience minimale "
         "(%d%% du flux) plutôt que de se limiter aux offres explicitement « débutant » — "
         "beaucoup d’entreprises ne filtrent pas aussi strictement qu’annoncé, surtout "
         "pour un profil motivé avec un projet ou un stage concret à montrer. Le "
         "<a href=\"/guide-stage-alternance-tech-paca.html\">stage et l’alternance</a> "
         "restent aussi le sas le plus direct vers un premier CDI, souvent chez la même "
         "entreprise.</p>" % pct_undisclosed),

        ("Le salaire d’un premier poste tech en PACA, ça donne quoi ?",
         "<p>Voir le détail dans le <a href=\"/guide-salaires-tech-paca.html\">guide des "
         "salaires tech PACA</a>, section junior&nbsp;: la médiane observée sur les postes "
         "de développement junior (moins de 2 ans) tourne autour de 45&nbsp;k€ brut par "
         "an, un chiffre étonnamment proche de celui des profils confirmés sur ce marché.</p>"),

        ("Comment repérer une offre qui accepte les débutants sans le dire explicitement ?",
         "<p>Ouvrir la description complète plutôt que de se fier au résumé&nbsp;: une "
         "fourchette d’expérience large (« 0 à 3 ans »), une formulation du type "
         "« autodidacte bienvenu » ou l’absence totale de mention d’ancienneté dans la "
         "section profil sont de meilleurs signaux qu’un simple filtre. En cas de doute, "
         "candidater reste la meilleure façon de vérifier — beaucoup d’annonces généralistes "
         "sont recyclées d’un recrutement à l’autre sans être ajustées au profil réellement "
         "reçu.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide premier emploi junior",
        h1="Premier emploi tech en PACA en 2026&nbsp;: comment décrocher un poste "
           "sans expérience&nbsp;?",
        intro="Quelles offres tech du Sud sont vraiment ouvertes aux débutants, chez "
              "quelles entreprises, et pour quels métiers&nbsp;? Chiffres calculés à "
              "partir des offres diffusées sur <a href=\"%s/\">sudtechjobs</a>." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/guide-stage-alternance-tech-paca.html">Guide stage & '
                   'alternance</a> · <a href="/guide-salaires-tech-paca.html">Guide des '
                   'salaires</a>.</p>',
        title="Premier emploi tech en PACA en 2026 : décrocher un poste sans "
              "expérience | sudtechjobs",
        description="Quelles offres tech en PACA sont vraiment accessibles aux "
                    "débutants, chez quelles entreprises et pour quels métiers, calculé "
                    "à partir des offres réellement diffusées sur sudtechjobs.")


SOPHIA_CITIES = ("Sophia Antipolis", "Nice", "Valbonne", "Cagnes-sur-Mer", "Biot")


def compute_sophia_stats(jobs):
    """Recomputed on every build, scoped to the Nice / Sophia Antipolis bassin."""
    rows = [j for j in jobs if j.get("city") in SOPHIA_CITIES]

    by_cat = Counter(j.get("category") for j in rows)
    by_company = Counter(j.get("company") for j in rows)

    sal = []
    for j in rows:
        p = _parse_salary_eur(j.get("salary"))
        if p:
            sal.append((p[0] + p[1]) / 2)

    remote_n = sum(1 for j in rows if _remote_bucket(j) and _remote_bucket(j) != "sur site")
    remote_known = sum(1 for j in rows if _remote_bucket(j))

    return {
        "n_total": len(jobs), "n": len(rows),
        "by_cat_top": [(cat, n) for cat, n in by_cat.most_common() if n > 0],
        "top_companies": [(c, n) for c, n in by_company.most_common(8) if n >= 2],
        "sal_n": len(sal), "sal_median": statistics.median(sal) if sal else None,
        "remote_n": remote_n, "remote_known": remote_known,
    }


def render_sophia_guide(jobs, generated):
    slug = "guide-emploi-tech-sophia-antipolis-cote-dazur"
    st = compute_sophia_stats(jobs)
    pct = round(100 * st["n"] / st["n_total"]) if st["n_total"] else 0

    cat_list = "".join(
        "<li><b>%s</b> — %d offre%s</li>" % (esc(CATS.get(cat, (cat,))[0]), n, "s" if n > 1 else "")
        for cat, n in st["by_cat_top"])
    company_txt = (", ".join("<b>%s</b> (%d)" % (esc(c), n) for c, n in st["top_companies"])
                   if st["top_companies"]
                   else "pas assez d’offres pour dégager des entreprises récurrentes en ce moment")
    pct_remote = (round(100 * st["remote_n"] / st["remote_known"])
                  if st["remote_known"] >= MIN_SAMPLE else None)
    sal_txt = (_fmt_keur(st["sal_median"]) if st["sal_n"] >= MIN_SAMPLE
               else "pas assez d’offres avec salaire affiché pour un chiffre fiable")

    faq = [
        ("Combien d’offres tech à Sophia Antipolis et sur la Côte d’Azur en ce moment ?",
         "<p>%d offres sur les %d actuellement diffusées sur sudtechjobs pour le bassin "
         "Nice / Sophia Antipolis / Valbonne / Biot (%d%% du flux PACA) — le deuxième "
         "bassin d’emploi tech de la région derrière l’axe Marseille / Aix-en-Provence.</p>"
         % (st["n"], st["n_total"], pct)),

        ("Quelles entreprises recrutent le plus sur ce bassin ?",
         "<p>%s. Le mix est révélateur&nbsp;: de gros industriels de la défense et des "
         "télécoms côtoient des ESN qui staffent leurs clients locaux — moins de "
         "startups pures que ce que l’image « tech park » de Sophia Antipolis suggère.</p>"
         % company_txt),

        ("Quels métiers dominent à Sophia Antipolis ?",
         "<p>La répartition par métier&nbsp;:</p><ul>%s</ul>"
         "<p>Le développement écrase largement le reste, avec une bonne part "
         "d’ingénierie systèmes/embarqué liée à l’activité télécom et défense du bassin.</p>"
         % cat_list),

        ("Le salaire est-il différent à Sophia Antipolis par rapport au reste de "
         "la PACA ?",
         "<p>La médiane observée sur ce bassin tourne autour de %s brut par an — voir le "
         "détail complet, par métier et par expérience, dans le <a href=\"/guide-salaires-"
         "tech-paca.html\">guide des salaires tech PACA</a>, qui ne montre pas d’écart "
         "marqué entre les grands bassins d’emploi de la région.</p>" % sal_txt),

        ("Le télétravail à Sophia Antipolis, c’est comment ?",
         "<p>%s</p>" % (
             "Sur les offres qui précisent leur politique, environ %d%% proposent au "
             "moins du télétravail ponctuel — voir le <a href=\"/guide-teletravail-tech-"
             "paca.html\">guide télétravail</a> pour la répartition détaillée par métier "
             "et par entreprise." % pct_remote
             if pct_remote is not None else
             "Pas assez d’offres précisant leur politique de télétravail sur ce bassin "
             "pour un chiffre fiable — voir le <a href=\"/guide-teletravail-tech-paca."
             "html\">guide télétravail</a> à l’échelle de toute la région.")),

        ("Qu’est-ce que French Tech Côte d’Azur, et à quoi ça sert pour chercher "
         "un job ?",
         "<p>French Tech Côte d’Azur est le réseau labellisé qui fédère les startups et "
         "scale-ups de Sophia Antipolis et de la région niçoise&nbsp;: annuaire "
         "d’entreprises, événements, mise en réseau. Beaucoup de ces startups ne publient "
         "pas systématiquement sur Welcome to the Jungle ni sur les job boards "
         "généralistes&nbsp;: consulter directement leur annuaire, ou aller à leurs "
         "meetups, reste un bon complément à une recherche par job board.</p>"),

        ("Sophia Antipolis, c’est vraiment un « pôle tech », ou surtout de "
         "l’industrie et des ESN ?",
         "<p>Les deux à la fois, et c’est important de le savoir avant de candidater&nbsp;: "
         "à côté des startups French Tech, une bonne partie du volume d’offres vient de "
         "grands comptes télécom/défense (ingénierie logicielle embarquée, systèmes, "
         "cybersécurité) et d’ESN qui y placent des consultants. Le poste « développeur à "
         "Sophia Antipolis » peut aussi bien être chez une startup de 15 personnes que "
         "chez un sous-traitant d’un grand groupe&nbsp;: à clarifier dès l’offre ou en "
         "entretien.</p>"),

        ("Comment suivre les nouvelles offres sur ce bassin spécifiquement ?",
         "<p>Les pages <a href=\"/emploi/sophia-antipolis.html\">Sophia Antipolis</a> et "
         "<a href=\"/emploi/nice.html\">Nice</a> listent les offres du moment, avec une "
         "alerte email possible sur cette recherche précise depuis la page d’accueil.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide Sophia Antipolis & Côte d’Azur",
        h1="Trouver un job tech à Sophia Antipolis et sur la Côte d’Azur en 2026",
        intro="Deuxième bassin d’emploi tech de la région derrière Marseille/Aix&nbsp;: "
              "qui recrute à Sophia Antipolis et à Nice, pour quels métiers, et à quel "
              "salaire&nbsp;? Chiffres calculés à partir des offres diffusées sur "
              "<a href=\"%s/\">sudtechjobs</a>." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/emploi/sophia-antipolis.html">Sophia Antipolis</a> · '
                   '<a href="/emploi/nice.html">Nice</a>.</p>',
        title="Trouver un job tech à Sophia Antipolis et Nice en 2026 | sudtechjobs",
        description="Qui recrute dans la tech à Sophia Antipolis et sur la Côte d'Azur, "
                    "pour quels métiers et à quel salaire, calculé à partir des offres "
                    "réellement diffusées sur sudtechjobs.")


def render_reconversion_guide(jobs, generated):
    """Unlike the other guides, there's no 'career change' field on a job posting —
    this one leans on general knowledge + the few real anchors the board does have
    (junior/débutant volume, stage & alternance), not a fresh stats breakdown."""
    slug = "guide-reconversion-tech-paca"
    jr = compute_junior_stats(jobs)
    it = compute_intern_stats(jobs)
    pct_undisclosed = round(100 * jr["n_undisclosed"] / jr["n_total"]) if jr["n_total"] else 0

    faq = [
        ("Une reconversion vers la tech est-elle réaliste en PACA, ou faut-il "
         "partir à Paris ?",
         "<p>Réaliste, mais avec un marché plus restreint qu’à Paris&nbsp;: moins "
         "d’offres au total, et une bonne part du volume junior/débutant vient d’ESN et "
         "de grands groupes (voir le <a href=\"/guide-premier-emploi-junior-tech-paca."
         "html\">guide premier emploi junior</a>) plutôt que de startups en forte "
         "croissance. Ce n’est pas un obstacle en soi — l’essentiel des embauches en "
         "reconversion se fait via ce type d’employeur, pas seulement en startup — mais "
         "le volume d’offres à cibler est mécaniquement plus réduit qu’en Île-de-France.</p>"),

        ("Faut-il repasser par une formation, ou peut-on candidater directement "
         "avec un projet perso ?",
         "<p>Les deux se voient. Un projet perso solide (une vraie application "
         "déployée, du code public, pas un simple tutoriel terminé) peut suffire à "
         "décrocher un entretien, surtout côté développement web&nbsp;; mais une formation "
         "structurée (bootcamp, titre professionnel, alternance) reste le chemin le plus "
         "prévisible pour la majorité, notamment parce qu’elle inclut souvent un stage ou "
         "une mission qui sert de premier CDI. %d%% des offres du site ne précisent "
         "aucune expérience minimale&nbsp;: beaucoup ne filtrent pas aussi strictement sur "
         "le diplôme ou le parcours qu’on pourrait le croire.</p>" % pct_undisclosed),

        ("Quelles formations reconversion existent dans le Sud ?",
         "<p>Plusieurs écoles et organismes forment au développement et à la data dans la "
         "région&nbsp;: 42 Nice, Epitech, Ynov, Simplon (souvent en alternance ou avec des "
         "frais réduits, orienté profils en reconversion) et Polytech pour un format plus "
         "académique. Comparer leur taux de retour à l’emploi réel et leurs partenariats "
         "entreprises locaux avant de s’engager reste le meilleur réflexe&nbsp;: tous ne se "
         "valent pas sur ce point.</p>"),

        ("Quels métiers sont les plus accessibles en reconversion ?",
         "<p>Le développement web reste le point d’entrée le plus courant&nbsp;: c’est "
         "aussi, de loin, le métier avec le plus d’offres débutant et de stages/"
         "alternances sur sudtechjobs (voir les guides "
         "<a href=\"/guide-premier-emploi-junior-tech-paca.html\">premier emploi</a> et "
         "<a href=\"/guide-stage-alternance-tech-paca.html\">stage &amp; alternance</a>). "
         "La data et le support technique (QA, no-code) sont d’autres portes d’entrée "
         "courantes, souvent perçues comme moins verrouillées sur un diplôme d’ingénieur "
         "que les postes de développement senior.</p>"),

        ("Quelles entreprises embauchent des profils en reconversion en PACA ?",
         "<p>Le board ne permet pas d’identifier ça directement&nbsp;: aucune offre ne se "
         "déclare « ouverte à la reconversion ». Une bonne indication indirecte&nbsp;: les "
         "entreprises qui recrutent le plus de profils débutants (ESN comme Meritis ou "
         "Capgemini, voir le <a href=\"/guide-premier-emploi-junior-tech-paca.html\">guide "
         "premier emploi</a>) ont en général des process de recrutement junior moins "
         "centrés sur le diplôme d’origine que sur les compétences démontrées en "
         "entretien technique — c’est une inférence raisonnable, pas une donnée mesurée.</p>"),

        ("Le diplôme compte-t-il vraiment moins en reconversion qu’ailleurs ?",
         "<p>Sur le papier, oui&nbsp;: la majorité des offres du site (plus de la moitié) "
         "ne précisent aucun niveau d’études. Dans les faits, dès qu’un niveau est "
         "affiché, c’est très souvent un bac+5&nbsp;— même sur des postes qui acceptent "
         "des débutants. Le diplôme pèse donc moins comme filtre explicite que comme "
         "défaut implicite du recruteur&nbsp;: à combler par un dossier de compétences "
         "concret plutôt qu’à contourner en espérant qu’il ne soit pas remarqué.</p>"),

        ("Combien de temps ça prend, entre la décision et le premier poste ?",
         "<p>Compter, dans les grandes lignes&nbsp;: quelques mois de formation intensive "
         "(bootcamp) à un an ou plus en alternance, puis une recherche de premier poste "
         "qui peut prendre de quelques semaines à plusieurs mois selon le réseau déjà "
         "construit pendant la formation — le stage ou la mission de fin de formation "
         "reste souvent le raccourci le plus direct vers un premier CDI.</p>"),

        ("Comment mettre toutes les chances de son côté avec sudtechjobs ?",
         "<p>Suivre les offres <a href=\"/guide-premier-emploi-junior-tech-paca.html\">"
         "premier emploi</a> et <a href=\"/guide-stage-alternance-tech-paca.html\">stage "
         "&amp; alternance</a> plutôt que de se limiter aux intitulés « développeur "
         "confirmé »&nbsp;; regarder les fiches entreprise pour repérer celles qui "
         "recrutent en volume (souvent plus ouvertes à un parcours atypique qu’une petite "
         "structure qui recrute un seul profil très spécifique)&nbsp;; et créer une alerte "
         "email sur une recherche large (métier + « débutant ») pour ne rien rater sans "
         "devoir repasser tous les jours.</p>"),
    ]

    return (slug,) + _render_faq_guide(
        slug=slug, breadcrumb="Guide reconversion",
        h1="Reconversion vers la tech en PACA en 2026&nbsp;: par où commencer&nbsp;?",
        intro="Formations, métiers accessibles, diplôme ou pas&nbsp;: ce que montrent "
              "les offres réellement diffusées sur <a href=\"%s/\">sudtechjobs</a>, et ce "
              "qu’il faut savoir en plus, quand on change de métier vers la tech dans le "
              "Sud." % SITE_URL,
        faq=faq, generated=generated,
        links_html='<p class="sub">Voir directement les offres&nbsp;? '
                   '<a href="/guide-premier-emploi-junior-tech-paca.html">Guide premier '
                   'emploi</a> · <a href="/guide-stage-alternance-tech-paca.html">Guide '
                   'stage &amp; alternance</a>.</p>',
        title="Reconversion vers la tech en PACA en 2026 : par où commencer | sudtechjobs",
        description="Formations, métiers accessibles, entreprises qui recrutent des "
                    "profils juniors : ce qu'il faut savoir pour une reconversion vers "
                    "la tech dans le Sud de la France.")


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


def _company_job_li(j):
    """same card as job_li but without the logo/company line — the page
    header already carries those for every job in the list."""
    city = j.get("city")
    city_txt = "Télétravail" if city == "Remote" else (city or "")
    co_html = ('<div class="co">%s</div>' % esc(city_txt)) if city_txt else ""
    return ('<li class="job co-job"><div class="t"><a href="../offre/{slug}.html">{title}</a></div>'
            '{co}{stack}<div class="meta">{meta}</div></li>').format(
        slug=j["_slug"], title=esc(j.get("title")), co=co_html,
        stack=_job_stack_html(j), meta=_job_meta_html(j))


def _company_list(jobs):
    return '<ul class="jobs">%s</ul>' % "".join(_company_job_li(j) for j in jobs)


def render_company(rec, jobs, generated, live_facets):
    slug = rec["slug"]
    name = rec["name"]
    canonical = "%s/entreprise/%s.html" % (SITE_URL, slug)
    p = rec.get("profile") or {}
    city = rec.get("city") or ""
    n = rec.get("open_roles", len(jobs))

    # ---- header: cover banner + big logo straddling it, then badges --------
    # real WTTJ cover photo when we have one; otherwise a tinted gradient banner
    # (deterministic per company, via a hue rotate) so no page ever looks bare
    if p.get("cover_image"):
        cover = '<div class="cover" style="background-image:url(%s)"></div>' % esc(p["cover_image"])
    else:
        hue = int(hashlib.sha1(name.encode()).hexdigest(), 16) % 360
        cover = '<div class="cover cover-fallback" style="--hue:%ddeg"></div>' % hue
    logo_html = _logo_html(rec.get("logo"), name, "lg")

    badges = []
    for eco in rec.get("ecosystems") or []:
        badges.append('<span class="eco">%s</span>' % esc(eco))
    for s in (p.get("sectors") or [])[:3]:
        badges.append("<span>%s</span>" % esc(s))
    if not p.get("sectors") and rec.get("category"):
        badges.append("<span>%s</span>" % esc(rec["category"]))
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
        city = f["h1"][len("Emplois tech à "):] if f["kind"] == "ville" else None
        with open(os.path.join(emploi_dir, fn), "w", encoding="utf-8") as fh:
            fh.write(render_facet(slug=slug, h1=f["h1"], intro=f["intro"],
                                  jobs=f["jobs"], siblings=siblings_for(slug, f),
                                  generated=generated, kind=f["kind"], city=city))

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
    with open(os.path.join(SITE, "a-propos.html"), "w", encoding="utf-8") as fh:
        fh.write(render_about())
    with open(os.path.join(SITE, "dashboard.html"), "w", encoding="utf-8") as fh:
        fh.write(render_dashboard(jobs, generated))

    # ---- guides (FAQ articles, site root + /guides/ hub) -------------------
    guides_meta = []
    for build_guide in (render_salary_guide, render_remote_guide, render_hiring_guide,
                        render_intern_guide, render_junior_guide, render_sophia_guide,
                        render_reconversion_guide):
        guide_slug, guide_html, card_title, card_desc = build_guide(jobs, generated)
        guides_meta.append((guide_slug, card_title, card_desc))
        with open(os.path.join(SITE, guide_slug + ".html"), "w", encoding="utf-8") as fh:
            fh.write(guide_html)
    guide_slugs = [g[0] for g in guides_meta]
    guides_dir = os.path.join(SITE, "guides")
    os.makedirs(guides_dir, exist_ok=True)
    with open(os.path.join(guides_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_guides_hub(guides_meta, generated))

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
    pages.append('<url><loc>%s/a-propos.html</loc><changefreq>monthly</changefreq>'
                 '<priority>0.5</priority></url>' % SITE_URL)
    pages.append('<url><loc>%s/dashboard.html</loc><lastmod>%s</lastmod>'
                 '<changefreq>daily</changefreq><priority>0.6</priority></url>'
                 % (SITE_URL, today))
    pages.append('<url><loc>%s/guides/</loc><lastmod>%s</lastmod><changefreq>daily</changefreq>'
                 '<priority>0.7</priority></url>' % (SITE_URL, today))
    for guide_slug in guide_slugs:
        pages.append('<url><loc>%s/%s.html</loc><lastmod>%s</lastmod><changefreq>daily</changefreq>'
                     '<priority>0.7</priority></url>' % (SITE_URL, guide_slug, today))
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

    # llms.txt — a short, curated map for LLM agents (llmstxt.org format).
    guide_lines = "\n".join("- [%s](%s/%s.html): %s" % (t, SITE_URL, s, d)
                            for s, t, d in guides_meta)
    llms = (
        "# sudtechjobs\n\n"
        "> Agrégateur d'offres d'emploi tech (dev, data, produit, design, stages et "
        "alternances) des entreprises de Provence-Alpes-Côte d'Azur, de Marseille à "
        "Sophia-Antipolis. %d offres en ligne, mises à jour chaque jour. Chaque offre "
        "renvoie vers la page carrière de l'employeur.\n\n"
        "## Données\n\n"
        "- [jobs.json](%s/jobs.json): toutes les offres actives, JSON\n"
        "- [feed.xml](%s/feed.xml): RSS des 50 offres les plus récentes\n"
        "- [sitemap.xml](%s/sitemap.xml): index de toutes les pages\n\n"
        "## Parcourir\n\n"
        "- [Offres par métier et ville](%s/emploi/): pages filtrées (métier, techno, ville, télétravail)\n"
        "- [Entreprises qui recrutent](%s/entreprise/): fiches des employeurs tech de la région\n"
        "- [Chiffres clés](%s/dashboard.html): instantané du marché (volumes, télétravail, salaires, top recruteurs)\n\n"
        "## Guides\n\n%s\n\n"
        "## À propos\n\n"
        "- [À propos](%s/a-propos.html): qui est derrière le site et d'où viennent les offres\n"
    ) % (len(jobs), SITE_URL, SITE_URL, SITE_URL, SITE_URL, SITE_URL, SITE_URL, guide_lines, SITE_URL)
    with open(os.path.join(SITE, "llms.txt"), "w", encoding="utf-8") as fh:
        fh.write(llms)

    # ---- RSS feeds (site/feed.xml + site/feed-dept-<dept>.xml) --------------
    # A machine-readable stream of the newest postings. Less a reader feature
    # than plumbing: Slack/Discord RSS bots in the PACA ecosystems can point a
    # channel at it, the social auto-posters read it as their "what's new"
    # source, and partners with their own (often stale, manually-fed) job page
    # — a French Tech chapter, a cluster — can import a département-scoped feed
    # to keep theirs fresh without anyone re-posting by hand.
    RSS_MAX = 50

    def _rfc822(s):
        try:
            dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)

    def _write_rss(job_list, filename, title, description):
        rss_jobs = sorted(
            job_list,
            key=lambda j: (j.get("first_seen") or j.get("published_at") or "", j["_slug"]),
            reverse=True,
        )[:RSS_MAX]
        items = []
        for j in rss_jobs:
            link = "%s/offre/%s.html" % (SITE_URL, j["_slug"])
            city = j.get("city") or ""
            is_remote = city == "Remote" or (j.get("remote") or "") in REMOTE_FULL
            cat_label = CATS.get(j.get("category"), (j.get("category"), ""))[0] or "Tech"
            meta = " · ".join(x for x in [
                cat_label, j.get("contract"),
                "télétravail" if is_remote else (city or None),
            ] if x)
            excerpt = re.sub(r"\s+", " ", (j.get("description_excerpt")
                                          or j.get("description") or "")).strip()
            if len(excerpt) > 300:
                excerpt = excerpt[:300].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
            desc = meta + ((" — " + excerpt) if excerpt else "")
            pub = _rfc822(j.get("first_seen") or j.get("published_at"))
            items.append(
                "<item>"
                "<title>%s — %s</title>"
                "<link>%s</link>"
                '<guid isPermaLink="true">%s</guid>'
                "%s"
                "<description>%s</description>"
                "</item>" % (
                    esc(j.get("title")), esc(j.get("company") or "—"),
                    esc(link), esc(link),
                    ("<pubDate>%s</pubDate>" % pub) if pub else "",
                    esc(desc)))
        rss = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
            "<channel>\n"
            "<title>%s</title>\n"
            "<link>%s/</link>\n"
            '<atom:link href="%s/%s" rel="self" type="application/rss+xml"/>\n'
            "<description>%s</description>\n"
            "<language>fr-FR</language>\n"
            "<lastBuildDate>%s</lastBuildDate>\n"
            "%s\n"
            "</channel>\n</rss>\n" % (
                esc(title), SITE_URL, SITE_URL, filename, esc(description),
                format_datetime(datetime.now(timezone.utc)),
                "\n".join(items)))
        with open(os.path.join(SITE, filename), "w", encoding="utf-8") as fh:
            fh.write(rss)
        return len(items)

    n_global = _write_rss(
        jobs, "feed.xml",
        "sudtechjobs — offres tech en PACA",
        "Les dernières offres dev, data, produit & design des entreprises tech "
        "du sud de la France.")

    dept_feed_counts = {}
    for code, (dslug, _dname, dwhere) in PACA_DEPTS.items():
        dept_jobs = [j for j in jobs
                     if CITY_DEPT.get(slugify(j.get("city") or "")) == code]
        if not dept_jobs:
            continue
        fname = "feed-dept-%s.xml" % dslug
        dept_feed_counts[fname] = _write_rss(
            dept_jobs, fname,
            "sudtechjobs — offres tech %s" % dwhere,
            "Les dernières offres dev, data, produit & design des entreprises "
            "tech %s (%s)." % (dwhere, code))

    # ---- prune stale files -------------------------------------------------
    wipe_html(offre_dir, offer_files | tombstone_files)
    wipe_html(emploi_dir, facet_files)
    wipe_html(entreprise_dir, company_files)

    print("render_pages: %d offers, %d tombstones, %d facet pages, %d company pages, "
          "%d sitemap urls, feed.xml (%d items), %s"
          % (len(offer_files), len(tombstone_files), len(facet_files),
             len(company_files), len(pages) + len(offers), n_global,
             ", ".join("%s (%d)" % (f, n) for f, n in dept_feed_counts.items())
             or "no dept feeds"),
          file=sys.stderr)


if __name__ == "__main__":
    main()
