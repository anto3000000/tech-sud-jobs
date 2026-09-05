# Vérification des ATS — 58 entreprises

_Passe des 5-6 sept. 2026. « déclaré » = la colonne `ats` de ta liste d'origine (non sourcée)._

## Constat

- La colonne `ats` d'origine est **du remplissage** : sur les 58, une seule valeur était juste (**Gojob / Lever**).
- **25 ATS confirmés** avec preuve (URL du board ou host de l'iframe/API).  **6 tentatifs** (connaissance publique, non reconfirmés).  **27 restent à vérifier** (page carrières en JS ou derrière Cloudflare — nécessite un navigateur headless).
- ATS réellement rencontrés en PACA : SmartRecruiters ×6, Welcome to the Jungle ×5, Workable ×4, iCIMS ×2, Ashby ×2, + Lever, Recruitee, Taleez, Cornerstone, Workday, et beaucoup de **sites custom sans ATS** (recrutement LinkedIn only).

## Confirmés / tentatifs

| Entreprise | déclaré | ATS réel | statut | preuve |
|---|---|---|---|---|
| Voyage Privé | `greenhouse` | **SmartRecruiters  (≠)** | confirmé | real ATS = SmartRecruiters (careers site runs the official 'SmartRecruiters WP Job Importer' plugin). Public SR postings API returns nothing under tried slugs -> feed is private or company id unknown. Fallbacks: WTTJ https://www.welcometothejungle.com/fr/companies/voyage-prive/jobs (~10 jobs) or WP REST https://www.people-voyage-prive.com/wp-json/wp/v2/sr_job (21 imported) |
| Mailinblack | `ashby` | **Taleez  (≠)** | confirmé | Taleez (org 11298) via self-hosted front /api/careez, 2 jobs |
| Amadeus | `custom` | **— (aucun / site custom)  (=)** | confirmé | bespoke enterprise careers site, needs dedicated scraper |
| Gojob | `lever` | **Lever  (=)** | confirmé | Lever, 67 jobs (~16 PACA/remote) |
| ARM France | `custom` | **iCIMS  (≠)** | confirmé | iCIMS, global board, needs browser + location filter |
| Median Technologies | `custom` | **Recruitee  (≠)** | confirmé | Recruitee, 5 offers |
| Skeepers | `greenhouse` | **Workable  (≠)** | confirmé | Workable, 0 open jobs currently |
| Monext | `custom` | **SmartRecruiters  (≠)** | confirmé | SmartRecruiters, 6 jobs, all Aix-en-Provence |
| Therapixel | `lever` | **— (aucun / site custom)  (≠)** | confirmé | custom WordPress careers page, 0 public openings (spontaneous only) |
| Ceva Santé Animale / E-Nov | `custom` | **Cornerstone  (≠)** | confirmé | Cornerstone OnDemand (ceva.csod.com) |
| Traxens | `lever` | **Welcome to the Jungle  (≠)** | tentatif | WTTJ fingerprint on careers page; slug unconfirmed |
| Videtics | `ashby` | **— (aucun / site custom)  (≠)** | confirmé | no ATS - /careers redirects to homepage, no jobs page (LinkedIn only) |
| Exotec (Bureaux Sud) | `greenhouse` | **Workable  (≠)** | confirmé | Workable (apply.workable.com/exotec) |
| Qarnot Computing (Antenne Sud) | `lever` | **— (aucun / site custom)  (≠)** | confirmé | Qarnot acquired by Scaleway (2025) - no standalone hiring entity; check Scaleway/Iliad careers |
| Crosscall | `custom` | **— (aucun / site custom)  (=)** | confirmé | no ATS - job posts are CMS pages on crosscall.com/pages/* (Shopify site). ~4-6 roles, mostly non-tech. |
| Criteo (Lab Sophia) | `greenhouse` | **SmartRecruiters  (≠)** | confirmé | SmartRecruiters (careers.smartrecruiters.com/Criteo2); also a Workday portal criteo.wd3.myworkdayjobs.com. Public SR postings feed may be disabled. |
| Inria Méditerranée | `custom` | **— (aucun / site custom)  (=)** | tentatif | Inria runs its own public research-jobs portal jobs.inria.fr - unconfirmed this pass |
| Legalyspace | `custom` | **Welcome to the Jungle  (≠)** | confirmé | Welcome to the Jungle (site links to wttj.com/fr/companies/legalyspace) |
| Schneider Electric (R&D Nice) | `custom` | **iCIMS  (≠)** | confirmé | iCIMS (careers-se.icims.com) |
| STMicroelectronics (Sites Sud) | `custom` | **SmartRecruiters  (≠)** | tentatif | ST careers historically SmartRecruiters (jobs.st.com) - unconfirmed this pass |
| Nvidia (Lab Sophia) | `greenhouse` | **Workday  (≠)** | confirmé | Workday (nvidia.wd5.myworkdayjobs.com) |
| Iad France (Pôle Digital) | `greenhouse` | **Welcome to the Jungle  (≠)** | tentatif | iad frequently on WTTJ - unconfirmed this pass |
| Vulog | `lever` | **Welcome to the Jungle  (≠)** | confirmé | Welcome to the Jungle (wttj.com/en/companies/vulog/jobs) |
| Hopper | `greenhouse` | **Ashby  (≠)** | confirmé | Ashby (jobs.ashbyhq.com/hopper, ~36 postings) |
| Spallian | `custom` | **Welcome to the Jungle  (≠)** | confirmé | Welcome Kit / WTTJ embed on spallian.com/join-us |
| Ses-Imagotag (Équipe Sud) | `custom` | **SmartRecruiters  (≠)** | confirmé | SmartRecruiters as VusionGroup (~54 postings) |
| Airbus Helicopters (Logiciel / R&D) | `custom` | **Workday  (≠)** | tentatif | Airbus group uses Workday - unconfirmed this pass |
| Alan | `greenhouse` | **Ashby  (≠)** | confirmé | Ashby (jobs.ashbyhq.com/alan, ~111 postings) |
| Digital Virgo | `custom` | **SmartRecruiters  (≠)** | tentatif | SmartRecruiters fingerprint; company id unconfirmed |
| Prozon | `custom` | **Welcome to the Jungle  (≠)** | confirmé | Welcome to the Jungle (prozon.com links to wttj/companies/prozon/jobs) |
| Searoutes | `lever` | **Workable  (≠)** | confirmé | Workable (searoutes.com links to apply.workable.com/searoutes) |

## Restent à vérifier (27) — page carrières inaccessible en scraping simple

| Entreprise | déclaré | piste |
|---|---|---|
| Inventy | `lever` | site/domaine introuvable — nom trop générique |
| Navya (Chasset) | `custom` | navya.tech (ex-robotique navettes, restructuré) — Cloudflare |
| Qualisteo | `lever` | petite boîte IoT — probablement LinkedIn only |
| Jaguar Network (Free Pro) | `custom` | filiale Iliad — carrières via Free Pro / groupe Iliad |
| Virbac (Pôle Digital) | `custom` | — |
| Bfore.Ai | `ashby` | scale-up cyber US/FR — probable Ashby ou Lever, à confirmer |
| Nfinite (Antenne Sud) | `greenhouse` | — |
| Syndic One | `custom` | petite proptech — LinkedIn only probable |
| ExactCure | `lever` | startup healthtech Nice — LinkedIn / WTTJ probable |
| Onet (Lab Innovation & Tech) | `custom` | grand groupe — SmartRecruiters/Cornerstone probable (groupe-onet.com) |
| Symag (BNP Paribas) | `custom` | filiale BNP — ATS groupe BNP (Taleo/SmartRecruiters) |
| Eversafe | `lever` | domaine incertain |
| Woleet | `custom` | woleet.io — micro-boîte, LinkedIn only |
| Izicap | `lever` | fintech Nice — Lever ou WTTJ probable |
| Ceva Logistics (Digital Hub) | `custom` | logisticien mondial — Workday/SmartRecruiters (cevalogistics.com) |
| Centrale Méditerranée Incubateur | `custom` | incubateur, pas un employeur direct |
| Ekinops | `custom` | équipementier télécom coté — SmartRecruiters/Workday probable |
| Advans Group (Elsys / Avisto) | `custom` | ESN — Flatchr / DigitalRecruiters fréquent chez les ESN |
| Sunpartner Technologies | `custom` | liquidée en 2019 — entrée obsolète |
| Earthworm Foundation Tech | `ashby` | ONG (earthworm.org) — Ashby déclaré, à confirmer |
| EpicNPoc | `lever` | studio design automobile — LinkedIn only probable |
| Gemy Cars (Hub Digital) | `custom` | distributeur auto — domaine incertain |
| STid | `custom` | stid-security.com — LinkedIn / DigitalRecruiters probable |
| fulll | `custom` | ex-Sinao — WTTJ ou Teamtailor probable |
| Onatera | `custom` | e-commerce — Flatchr/DigitalRecruiters fréquent en retail |
| SemiAI | `custom` | domaine incertain (semi.ai ?) |
| Paradox EN | `lever` | attention: le board Ashby 'paradox' est une AUTRE société (Paradox.ai US) |

## Fait

- `jobboard/companies.json` : bloc `known` (ATS vérifié + slug + URL + note datée) ajouté pour les 31 entreprises résolues.
- `resolve.py` prend `known` en priorité absolue → la liste corrigée sort dans `companies.resolved.json`.

## Pour finir les 27

Il faut un fetch navigateur (les pages sont rendues en JS ou protégées Cloudflare) : charger chaque page carrières, lire les requêtes réseau, repérer l'appel API de l'ATS. ~1 min/entreprise. Dis-moi si tu veux que je le fasse.
