# Tech Sud — job board tech PACA

Agrégateur d'offres **Tech / Data / Product / Design** en Provence-Alpes-Côte
d'Azur. Zéro scraping HTML fragile : on tape les mêmes API publiques que les
sites sources.

## Les 3 couches

| # | Source | Ce qu'on en tire | Script |
|---|--------|------------------|--------|
| 1 | **Welcome to the Jungle** — index Algolia public `wk_cms_jobs_production` | ~5 300 offres PACA actives, filtrées Tech via classifieur de titres | `sources/wttj.py` |
| 1b | **WTTJ detail API** `api.welcometothejungle.com/api/v1/organizations/<org>/jobs/<slug>` | stack technique (`tools`), description, niveau d'xp, lien de candidature direct, logo | `sources/wttj_enrich.py` |
| 2 | **Annuaire French Tech Aix-Marseille** — WordPress REST `/wp-json/wp/v2/annuaire` | ~660 boîtes + domaine (résolu via le lien "site" de leur fiche) | `annuaire/frenchtech_amp.py` |
| 3 | **ATS des boîtes** (Ashby, Lever, SmartRecruiters, Taleez, Recruitee, Workable, Greenhouse, Personio) | offres en direct de l'employeur, lien de candidature natif | `resolve.py` + `fetch_jobs.py` |

La couche 2 alimente la couche 3 : on part de la liste d'entreprises, on
détecte l'ATS (`resolve.py` ping les boards + fingerprint la page carrières),
puis `fetch_jobs.py` récupère les offres via l'API JSON de chaque ATS.

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

# 2. annuaire French Tech Aix-Marseille (~5 min avec résolution de domaine)
python3 jobboard/annuaire/frenchtech_amp.py      # -> data/companies.frenchtech-amp.json

# 3. ATS : résoudre puis récupérer
python3 jobboard/resolve.py                      # companies.json (+ annuaire) -> companies.resolved.json
python3 jobboard/fetch_jobs.py -o jobboard/data/ats_jobs.json

# 4. fusion -> feed unique du site
python3 jobboard/build.py                        # -> site/jobs.json  (+ data/jobs.json)

# 5. servir le site statique
cd jobboard/site && python3 -m http.server 8777  # http://localhost:8777
```

`build.py` : normalise, garde PACA + Tech/Data/Product, dé-duplique
(`entreprise + intitulé` ; un lien ATS direct l'emporte sur un lien WTTJ),
trie par date.

## Déploiement (GitHub Actions + Pages)

`.github/workflows/jobboard.yml` — tous les jours ~07 h (Paris) + à chaque push
sur `jobboard/**` + manuel (`workflow_dispatch`) :

1. `./jobboard/pipeline.sh` (wttj → enrich → ats best-effort → build)
2. commit du feed rafraîchi (`data/*.json`, `site/jobs.json`) avec `[skip ci]`
   — c'est ce qui fait persister `seen.json` d'un run à l'autre (badge « nouveau »)
3. déploiement de `jobboard/site/` sur GitHub Pages

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
- Couche 2 bis : annuaire French Tech Côte d'Azur (Sophia / Nice) — le site
  renvoie 403 sur `curl`, à récupérer via navigateur headless ou leur API.
- Couche 3 : beaucoup de slugs ATS non vérifiés (404). Alimenter `resolve.py`
  avec `data/companies.frenchtech-amp.json` (605 domaines) pour du fingerprint
  réel plutôt que des devinettes.
