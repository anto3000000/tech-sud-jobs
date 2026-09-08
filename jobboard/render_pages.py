#!/usr/bin/env python3
"""Layer 5 — static SEO pages.

Reads jobboard/site/jobs.json (built by build.py) and writes, into jobboard/site/:

    offre/<slug>.html     one page per job — JobPosting JSON-LD, OpenGraph, canonical
    emploi/<facet>.html   filtered list pages (métier × ville, techno × ville, télétravail…)
    emploi/index.html     hub linking every facet page
    sitemap.xml           every URL above + the home
    robots.txt            points crawlers at the sitemap

All of this is build output: git-ignored, regenerated on every run, deployed from
the GitHub Pages artifact (not from git). The SPA home (index.html) is untouched.

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
FEED = os.path.join(SITE, "jobs.json")

SITE_URL = os.environ.get(
    "SITE_URL", "https://sudtechjobs.com"
).rstrip("/")

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
h2{font-family:"Bricolage Grotesque",sans-serif;font-size:15px;margin:26px 0 8px}
ul.jobs{list-style:none;margin:0;padding:0}
ul.jobs li{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px;
 margin:0 0 11px;box-shadow:var(--shadow)}
ul.jobs li a{font-family:"Bricolage Grotesque",sans-serif;font-weight:600;color:var(--ink);font-size:15px}
ul.jobs .co{color:var(--muted);font-size:13px;margin:3px 0 0}
footer{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
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
  job board tech du sud de la France
  <br><br>Une offre à ajouter, une remarque, ou juste envie de papoter du Sud&nbsp;?
  Écrivez-moi, ça fait toujours plaisir 🫰
  <a href="mailto:hello@sudtechjobs.com">✉️ hello@sudtechjobs.com</a>
</footer>
</div>
</body>
</html>""".format(
        title=esc(title), desc=esc(description), canon=esc(canonical),
        css=CSS, head_extra=head_extra, body=body,
        home=SITE_URL + "/", hub=SITE_URL + "/emploi/",
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
    apply_href = j.get("apply_url") or j.get("url") or ""

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
        "<p>Offre publiée par %s%s. Consultez l'annonce complète et postulez en "
        "direct via le bouton ci-dessus.</p>" % (
            esc(j.get("company")), " à " + esc(city) if city and not is_remote else ""))
    profile_html = ""
    if j.get("profile_excerpt"):
        profile_html = "<h2>Profil recherché</h2>\n" + text_to_html(j["profile_excerpt"])
    benefits_html = ""
    if j.get("benefits_preview"):
        benefits_html = "<h2>Avantages</h2>\n<ul>%s</ul>" % "".join(
            "<li>%s</li>" % esc(b) for b in j["benefits_preview"])

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
            '<li><a href="%s.html">%s</a><div class="co">%s%s</div></li>' % (
                s["_slug"], esc(s["title"]), esc(s.get("company")),
                " · " + esc(s.get("city")) if s.get("city") else "")
            for s in items)

    sim_html = ""
    if similar:
        sim_html = "<h2>Offres similaires</h2>\n" + _job_list(similar)
    if same_company:
        sim_html += "\n<h2>Autres offres chez %s</h2>\n%s" % (
            esc(j.get("company")), _job_list(same_company))

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
    }
    if posted:
        ld["datePosted"] = posted[:10]
        try:
            d0 = datetime.fromisoformat(posted.replace("Z", "+00:00"))
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
  <a class="apply" href="{apply}" target="_blank" rel="nofollow noopener">Postuler{ats}</a>
</div>
<div class="desc">{desc}</div>
{profile}
{benefits}
{facet_link}
{similar}
""".format(
        home=SITE_URL + "/", catslug=slugify(cat or "tech"), catlabel=esc(cat_label),
        title=esc(j.get("title")), company=esc(j.get("company")),
        cityline=(" — télétravail" if is_remote else (" — " + esc(city) if city else "")),
        krow=krow, stack=stack_html, apply=esc(apply_href),
        ats=(" sur " + esc(j["ats"].title())
             if j.get("ats") and j["ats"].lower() != "external" else " en direct"),
        desc=desc_html, profile=profile_html, benefits=benefits_html,
        facet_link=facet_link, similar=sim_html,
    )
    head_extra = jsonld(ld) + "\n" + jsonld(crumbs)
    return shell(
        title="%s — %s%s | sudtechjobs" % (
            j.get("title"), j.get("company"),
            ", " + city if city and city != "Remote" else ""),
        description=meta_desc, canonical=canonical, head_extra=head_extra, body=body,
    )


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

    offre_dir = os.path.join(SITE, "offre")
    emploi_dir = os.path.join(SITE, "emploi")
    os.makedirs(offre_dir, exist_ok=True)
    os.makedirs(emploi_dir, exist_ok=True)

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

    # ---- write facet pages ------------------------------------------------
    FAMILY = {
        "métier": ("métier", "métier×ville"),
        "métier×ville": ("métier", "métier×ville"),
        "ville": ("ville",),
        "techno": ("techno", "techno×ville"),
        "techno×ville": ("techno", "techno×ville"),
        "télétravail": ("télétravail", "métier×télétravail"),
        "métier×télétravail": ("télétravail", "métier×télétravail"),
    }

    def siblings_for(slug, f):
        want = FAMILY.get(f["kind"], (f["kind"],))
        toks = set(slug.split("-"))
        fam = [(s2, f2["h1"].replace("Emplois ", "").replace("tech à ", ""))
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
        ("Par métier", grp(("métier", "métier×ville"), "Emplois ")),
        ("Par techno", grp(("techno", "techno×ville"), "Emplois ")),
        ("Télétravail", grp(("télétravail", "métier×télétravail"), "Emplois ")),
    ]
    with open(os.path.join(emploi_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_hub(groups, generated))
    facet_files.add("index.html")

    # ---- sitemap + robots ----------------------------------------------
    today = datetime.now(timezone.utc).date().isoformat()
    urls = ['<url><loc>%s/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>'
            % SITE_URL]
    urls.append('<url><loc>%s/emploi/</loc><changefreq>daily</changefreq><priority>0.8</priority></url>'
                % SITE_URL)
    for slug in sorted(live_facets):
        urls.append('<url><loc>%s/emploi/%s.html</loc><lastmod>%s</lastmod>'
                    '<changefreq>daily</changefreq><priority>0.7</priority></url>'
                    % (SITE_URL, slug, today))
    for j in jobs:
        lm = (j.get("first_seen") or j.get("published_at") or today)[:10]
        urls.append('<url><loc>%s/offre/%s.html</loc><lastmod>%s</lastmod>'
                    '<changefreq>weekly</changefreq><priority>0.6</priority></url>'
                    % (SITE_URL, j["_slug"], lm))
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "\n".join(urls) + "\n</urlset>\n")
    with open(os.path.join(SITE, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write(sitemap)
    with open(os.path.join(SITE, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % SITE_URL)

    # ---- prune stale files -------------------------------------------------
    wipe_html(offre_dir, offer_files)
    wipe_html(emploi_dir, facet_files)

    print("render_pages: %d offers, %d facet pages, sitemap %d urls"
          % (len(offer_files), len(facet_files), len(urls)), file=sys.stderr)


if __name__ == "__main__":
    main()
