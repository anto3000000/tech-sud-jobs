# Tech Sud — job board tech PACA

Agrégateur d'offres **Tech / Data / Product / Design** en Provence-Alpes-Côte
d'Azur. Zéro scraping HTML fragile : on tape les mêmes API publiques que les
sites sources.

## Les couches

| # | Source | Ce qu'on en tire | Script |
|---|--------|------------------|--------|
| 1 | **Welcome to the Jungle** — index Algolia public `wk_cms_jobs_production` | ~5 300 offres PACA actives, filtrées Tech via classifieur de titres | `sources/wttj.py` |
| 1b | **WTTJ detail API** `api.welcometothejungle.com/api/v1/organizations/<org>/jobs/<slug>` | stack technique (`tools`), description, niveau d'xp, lien de candidature direct, logo | `sources/wttj_enrich.py` |
| 1c | **France Travail** — API officielle *Offres d'emploi v2* | offres PACA des 6 dép. sur `grandDomaine=M18` (info & télécoms), filtrées Tech ; employeur masqué / intérim / ESN écartés ; **lien de candidature sur un ATS connu** (pas d'agrégateur type Meteojob) ; **cap 5 offres / employeur** (anti-régie) | `sources/francetravail.py` |
| 2 | **Annuaire French Tech Aix-Marseille** — WordPress REST `/wp-json/wp/v2/annuaire` | ~660 boîtes + domaine (résolu via le lien "site" de leur fiche) | `annuaire/frenchtech_amp.py` |
| 2b | **Annuaire French Tech Côte d'Azur** (Sophia / Nice) — CPT `portfolio` non exposé en REST, lu depuis la grille Nectar de la page *Nos Start-up* | ~100 boîtes + domaine (le lien de la carte = le site de la boîte) | `annuaire/frenchtech_cotedazur.py` |
| 2c | **Telecom Valley** (cluster Sophia) — WordPress REST `/wp-json/wp/v2/project` | ~125 membres + domaine (texte de l'extrait) + tags territoire / structure / secteur | `annuaire/telecom_valley.py` |
| 2d | **Aktantis** (ex-Pôle SCS, deeptech PACA) — archive WordPress `/annuaire-des-membres/page/N/` | ~275 membres PACA + domaine + `data-zone` / `data-techno` (µélectronique, IoT, IA, cyber, photonique) | `annuaire/aktantis.py` |
| 2e | **Medinsoft** (Marseille / Aix) — collection Wix Data `Annuaire` dans le blob `wix-warmup-data` | poignée de boîtes seulement (collection publique à peine peuplée depuis déc. 2024) | `annuaire/medinsoft.py` |
| 3 | **ATS des boîtes** (Ashby, Lever, SmartRecruiters, Taleez, Recruitee, Workable, Greenhouse, Personio) | offres en direct de l'employeur, lien de candidature natif | `resolve.py` + `fetch_jobs.py` |
| 4 | **Profils entreprise** — feed + cache WTTJ + annuaires | 1 ligne par boîte qui recrute : agrégats des offres (postes ouverts, split métier/contrat/ville, stack consolidée, rythme d'embauche, avantages) + profil WTTJ **fr** (description, effectif, secteur, création, siège, parité, réseaux, lien WTTJ) + type d'employeur dérivé (startup / scale-up / ETI / grand groupe / ESN) + écosystème / ATS résolu | `build_companies.py` → `site/companies.json` |

Les couches 2* alimentent la couche 3 : `merge_companies.py` concatène la liste
curée + tous les annuaires (dédup domaine puis nom, la source la plus fiable
gagne) → `data/companies.all.json`. Puis `resolve.py` détecte l'ATS (ping des
boards + fingerprint de la page carrières) et `fetch_jobs.py` récupère les
offres via l'API JSON de chaque ATS. Les scrapers d'annuaire sont stdlib pur ;
un User-Agent de navigateur suffit (cf. `annuaire/_common.py`) — pas besoin de
navigateur headless, sauf si un hôte se remet à renvoyer 403 en CI (dans ce cas
`--html-file` pour parser une copie récupérée à la main).

**Vérification géo (couche 3).** Une sonde directe *devine* le slug depuis le
nom de la boîte → un nom générique (« Blue », « Tempo », « CM ») tombe sur le
board d'un homonyme étranger. `resolve.py` ne garde donc un hit `direct-probe`
que si (a) un humain a déclaré ce slug exact, ou (b) au moins une offre du board
est localisée en France. Sinon → `needs_review` + `resolved_via=direct-probe-unverified`,
et `fetch_jobs.py` l'ignore (`--include-unverified` pour forcer ; un second
garde-fou géo re-vérifie les offres réellement récupérées).

Couche 1c : c'est une source d'*offres*, pas de *boîtes* — on récupère
l'annonce et son lien de candidature directement, donc pas de slug à deviner,
pas de risque d'homonyme. Au `build`, un lien ATS direct > lien WTTJ > lien
France Travail (agrégateur).

## Pipeline

```bash
# 1. aspirateur WTTJ (rapide, ~7 s)
python3 jobboard/sources/wttj.py                 # -> data/wttj_paca.json  (tech only)
python3 jobboard/sources/wttj.py --all           #    toutes professions
python3 jobboard/sources/wttj.py --no-adjacent   #    sans IT support / consulting / tech sales

# 1 bis. enrichissement : stack technique, description, xp, apply URL direct, logo
python3 jobboard/sources/wttj_enrich.py          # complète data/wttj_paca.json en place (~30 s, puis cache)
#   -> réponses cachées dans data/cache/wttj/ ; re-runs = réseau seulement pour les offres nouvelles
#   -> re-classe chaque offre avec la liste `tools` (titre vague + stack dev => eng/data)

# 1 ter. France Travail (API officielle ; ~10 s)
#   creds : jobboard/.env  ->  FT_CLIENT_ID=... / FT_CLIENT_SECRET=...  (app sur https://francetravail.io)
python3 jobboard/sources/francetravail.py        # -> data/francetravail_paca.json
#   défauts : tech only ; sans employeur masqué / intérim / ESN ; lien de candidature
#   sur un ATS connu uniquement (--links direct) ; max 5 offres par employeur
python3 jobboard/sources/francetravail.py --links no-aggregator --max-per-company 0  # plus permissif
python3 jobboard/sources/francetravail.py --keep-agencies --keep-anonymous --links any  # tout garder
python3 jobboard/sources/francetravail.py --rome M1805,M1806,M1810           # cibler des codes ROME précis

# 2. annuaires institutionnels / clusters (chacun -> data/companies.<source>.json)
python3 jobboard/annuaire/frenchtech_amp.py        # French Tech Aix-Marseille (~5 min, résout les domaines)
python3 jobboard/annuaire/frenchtech_cotedazur.py  # French Tech Côte d'Azur / Sophia-Nice (~100)
python3 jobboard/annuaire/telecom_valley.py        # cluster Telecom Valley, Sophia (~125)
python3 jobboard/annuaire/aktantis.py              # Aktantis / ex-Pôle SCS, deeptech PACA (~275 ; --all-regions pour Occitanie)
python3 jobboard/annuaire/medinsoft.py             # Medinsoft, Marseille/Aix (collection publique quasi vide)

# 3. ATS : fusionner curated + tous les annuaires, résoudre, récupérer
python3 jobboard/merge_companies.py              # -> data/companies.all.json  (~1140 boîtes, dédup domaine/nom)
python3 jobboard/resolve.py -i jobboard/data/companies.all.json \
        -o jobboard/companies.resolved.json --workers 20      # détecte ATS + slug (~25 min, vérif géo incluse)
python3 jobboard/fetch_jobs.py -o jobboard/data/ats_jobs.json

# 4. fusion -> feed unique du site
python3 jobboard/build.py                        # -> site/jobs.json  (+ data/jobs.json)

# 4 bis. profils entreprise (1 ligne par boîte qui recrute)
python3 jobboard/build_companies.py              # -> site/companies.json
#   agrège les offres de chaque boîte (postes ouverts, split métier/contrat/ville,
#   stack consolidée, rythme d'embauche) + profil WTTJ distillé depuis data/cache/wttj/
#   (effectif, secteur, année de création, siège, parité, réseaux, cover) + appartenance
#   écosystème / ATS résolu (match domaine/nom sur data/companies.all.json + companies.resolved.json)

# 5. pages statiques SEO (offre par offre + listes filtrées + fiches entreprise + sitemap)
python3 jobboard/render_pages.py                 # -> site/offre/*.html, site/emploi/*.html, site/entreprise/*.html, sitemap.xml, robots.txt

# 6. servir le site statique
cd jobboard/site && python3 -m http.server 8777  # http://localhost:8777
```

`build.py` : normalise, garde PACA + Tech/Data/Product, dé-duplique
(`entreprise + intitulé` ; ordre de préférence du lien : ATS direct > WTTJ >
France Travail), trie par date. **Garde-fou** : si le merge sort moins de
`MIN_JOBS` offres (défaut 150) il quitte en erreur au lieu d'écrire un feed
quasi vide — `ALLOW_SMALL_FEED=1` pour forcer en local.

## Déploiement (GitHub Actions + Pages)

`.github/workflows/jobboard.yml` — tous les jours ~07 h (Paris) + à chaque push
sur `jobboard/**` + manuel (`workflow_dispatch`) :

1. `./jobboard/pipeline.sh` (wttj → enrich → **france travail** → ats best-effort → build → **render_pages**)
2. commit du feed rafraîchi (`data/*.json`, `site/jobs.json`) avec `[skip ci]`
   — c'est ce qui fait persister `seen.json` d'un run à l'autre (badge « nouveau »)
3. déploiement de `jobboard/site/` sur GitHub Pages — l'artefact inclut les
   pages statiques régénérées à l'étape 1 (`site/offre/`, `site/emploi/`,
   `sitemap.xml`), qui sont *git-ignorées* : jamais commitées, reconstruites à
   chaque run.

> Couche 1c (France Travail) : ajouter `FT_CLIENT_ID` / `FT_CLIENT_SECRET` dans
> les *repository secrets* et les exporter dans le job du workflow. Sans eux
> l'étape est sautée (best-effort), le reste du pipeline tourne.

Le cache des réponses détail WTTJ est porté par `actions/cache` (`data/cache/`),
donc chaque run ne re-télécharge que les offres nouvelles.

Repo dédié : **`anto3000000/tech-sud-jobs`** (public). Pages configuré avec
« Source : GitHub Actions ». Le site : `https://anto3000000.github.io/tech-sud-jobs/`.
Chaque push sur `jobboard/**` relance le workflow ; sinon `gh workflow run
jobboard.yml -R anto3000000/tech-sud-jobs`.

## Classifieur (`classify.py`)

WTTJ n'a une taxonomie `profession` que sur ~12 % des annonces agrégées : on
ne peut pas s'y fier. `classify(titre)` renvoie `eng` / `data` / `product` /
`design` / `tech-adjacent` / `None` à partir du titre (règles regex FR+EN,
liste de négatifs pour tuer les faux positifs BTP / maintenance indus /
commercial non-tech). `python3 jobboard/classify.py` lance les tests.

## Le front (`site/index.html`)

Un seul fichier, vanilla JS, aucune dépendance. Lit `jobs.json`, filtres :
recherche plein-texte, ville, contrat, catégorie, télétravail, tri.
Déployable tel quel sur n'importe quel hébergement statique (Pages, Netlify,
S3…) — il suffit d'y déposer `index.html` + `jobs.json` régénéré par un cron.

## SEO — pages statiques (`render_pages.py`)

Le front est une SPA à une seule URL : invisible pour Google. `render_pages.py`
lit `site/jobs.json` (déjà construit par `build.py`) et écrit, dans `site/` :

| Sortie | Quoi |
|--------|------|
| `offre/<slug>.html` | une page par offre — `<title>` / OpenGraph / **JSON-LD `JobPosting`** (Google for Jobs : `title`, `description` HTML complète, `datePosted`, `validThrough`, `hiringOrganization`, `jobLocation` **ou** `jobLocationType: TELECOMMUTE` + `applicantLocationRequirements`, `employmentType`, `baseSalary`, `identifier`), fil d'ariane, offres similaires, lien vers la liste filtrée |
| `offre/<slug>.html` *(pierre tombale)* | quand une offre sort du feed, la page est **conservée** en `noindex, follow` + `<meta refresh>` / JS vers la facette métier, **sans** balisage `JobPosting` — retirée du sitemap. Purgée après `TOMBSTONE_DAYS` (120 j) → 404. État dans `data/offer_index.json` (commit CI, comme `seen.json`) |
| `emploi/<facette>.html` | listes pré-rendues : métier (`eng`, `data`, `product`…), ville, **métier × ville** (`eng-marseille`), **département** (`dept-bouches-du-rhone`, agrège les villes via `CITY_DEPT`), techno (`stack-react`), **techno × ville**, télétravail. Seuil : ≥ 3 offres (`MIN_FACET`), ≥ 8 pour une techno seule (`MIN_STACK`) |
| `emploi/index.html` | hub qui pointe vers toutes les facettes |
| `entreprise/<slug>.html` | une fiche par boîte qui recrute (source : `site/companies.json`, cf. `build_companies.py`) — en-tête bannière (cover WTTJ) + gros logo, badges écosystème / secteur, carte de chiffres (type d'employeur, effectif, création, siège, parité, index égalité, ATS, site, lien WTTJ), description WTTJ **en français**, sparkline du rythme d'embauche, **stack consolidée** (liens vers `stack-*`), split par métier / contrat / ville (liens vers les facettes), avantages mentionnés, salaires affichés, liste des offres ouvertes. JSON-LD `Organization` + `ItemList` + `BreadcrumbList`. Pages offre et SPA lient le nom de la boîte vers sa fiche |
| `entreprise/index.html` | hub qui liste toutes les entreprises (nb d'offres, ville, secteur, écosystème) |
| `mentions-legales.html`, `cgu.html`, `confidentialite.html` | pages légales statiques (racine du site) : éditeur non pro + hébergeur, CGU de l'agrégateur, politique RGPD (Umami sans cookie). Texte figé (`LEGAL_UPDATED`), rebâti à chaque run. Lien en pied de page + bandeau d'info audience (sans consentement, Umami étant cookieless) sur toutes les pages et la SPA |
| `sitemap.xml` | **index** → `sitemap-pages.xml` (home + facettes + fiches entreprise) + `sitemap-offres.xml` (offres vivantes seules, `lastmod` = première vue). Google for Jobs découvre les `JobPosting` via ce dernier |
| `feed.xml` | RSS 2.0 des **50 offres les plus récentes** (tri par `first_seen`), lien vers la page `offre/` (pas l'ATS, pour garder le clic sur le site). Pas une feature lecteur : c'est le format d'entrée des bots RSS Slack/Discord des écosystèmes PACA et des auto-posts Twitter/LinkedIn. Feed global uniquement ; les feeds par facette viendront si besoin. Autodiscovery `<link rel="alternate">` dans le `<head>` de toutes les pages statiques |
| `robots.txt` | pointe l'index sitemap |

Dé-doublonnage inter-sources (même offre vue via WTTJ *et* son ATS) : `build.py`
`dedupe()` clé `(entreprise, titre normalisé)` + rang de lien
`ATS direct > WTTJ > France Travail` — vérifié, 0 doublon cross-source résiduel.

Tout est du **build output** : `.gitignore`-é, reconstruit à chaque run,
déployé depuis l'artefact Pages (pas depuis git) — `site/companies.json` inclus
(intermédiaire lu par `render_pages.py`, pas commité, mais poussé dans l'artefact
Pages avec le reste de `site/`). La SPA `index.html` est quasi intacte : `<head>`
SEO, `<nav>` vers le hub, et le nom de l'entreprise sur chaque carte lie vers sa
fiche (`companySlug()` en JS, aligné sur `render_pages.slugify`).

Base des URL : `SITE_URL` (défaut `https://anto3000000.github.io/tech-sud-jobs`).
Les liens internes sont relatifs (marchent quel que soit le domaine) ;
`canonical` / OG / `sitemap` sont absolus.

**À faire côté Google** :
1. Search Console → propriété `https://sudtechjobs.com/`, soumettre
   `https://sudtechjobs.com/sitemap.xml` (l'index ; les deux enfants sont lus
   automatiquement).
2. Rich Results Test sur une page `offre/` pour confirmer que le `JobPosting`
   passe, puis surveiller *Améliorations → Offres d'emploi* dans la Search
   Console (erreurs, offres valides, expirées).
3. Google for Jobs exige que les offres fermées disparaissent : c'est le rôle des
   pierres tombales `noindex` + du retrait du `sitemap-offres.xml` ci-dessus. Un
   vrai `410`/`301` est impossible sur Pages (hébergement statique) — le
   `noindex` + `<meta refresh>` en est l'équivalent praticable.

`robots.txt` est désormais servi à la racine du domaine perso (`sudtechjobs.com`)
donc bien pris en compte.

## Clés WTTJ

`sources/wttj.py` embarque la clé de recherche publique (restreinte par
referer, donc sans risque). Si un run commence à renvoyer 403, elle a tourné :
`--refresh-keys` la re-scrape depuis `https://www.welcometothejungle.com/api/env`.

## Étendre à d'autres régions

```bash
python3 jobboard/sources/wttj.py --state "Occitanie"       -o jobboard/data/wttj_occ.json
python3 jobboard/sources/wttj.py --state "Auvergne-Rhone-Alpes" -o jobboard/data/wttj_ara.json
```

(valeur exacte de la facette `offices.state`, sans accents — cf. `STATE_PACA`).

## À faire

- **Déploiement + automatisation** : GitHub Action cron quotidien → pipeline →
  commit `site/jobs.json` → deploy Pages/Netlify. Le cache `data/cache/wttj/`
  rend les runs suivants quasi gratuits.
- **Date de première vue** : persister l'ensemble des `objectID` vus + un
  `first_seen` par offre (le `published_at` WTTJ est parfois une re-publication),
  et exposer un badge "nouveau" / un filtre "ajoutées cette semaine".
- ~~Couche 2 bis : annuaire French Tech Côte d'Azur (Sophia / Nice)~~ ✅ fait
  (`annuaire/frenchtech_cotedazur.py`) — plus Telecom Valley, Aktantis (ex-Pôle
  SCS) et Medinsoft. Un UA de navigateur passe le 403, pas de headless.
- French Tech Côte d'Azur : la grille publique plafonne à 100 des ~208 fiches
  `portfolio` (triées A→Z). Le reste passe par l'archive
  `/?post_type=portfolio&paged=N`, mais la plupart de ces fiches n'ont pas de
  lien "site" → elles ne donneraient au résolveur que des devinettes de domaine.
- Medinsoft : la collection Wix `Annuaire` n'a que ~4 lignes publiques
  (lancée déc. 2024). Re-scraper quand `datasetSize.total` grimpe.
- Couche 3 : beaucoup de slugs ATS non vérifiés (404). Le fingerprint réel
  tourne maintenant sur ~1050 domaines (`data/companies.all.json`).
- **Classifieur — faux positifs `eng`** : « Chargé de **développement** commercial »,
  « Responsable **développement** foncier », « Chargé de **développement** RH »
  passent en catégorie *Développeur* (le mot français). Peu visible dans la SPA,
  mais chaque page `emploi/eng-*.html` les référence maintenant sous « Emplois
  Développeur à … ». Ajouter à `classify.py` un négatif sur
  `d[ée]veloppement (commercial|rh|foncier|immobilier|des ventes|de la client)`.
- SEO : après indexation, générer des pages **entreprise** (`entreprise/<slug>.html`,
  toutes les offres d'une boîte + JSON-LD `Organization`) et **`validThrough`**
  réel plutôt que `datePosted + 90 j`.
