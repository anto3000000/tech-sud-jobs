# sudtechjobs — worker d'inscription aux alertes

Petit endpoint Cloudflare Workers qui reçoit `{ email, search, search_label }`
depuis le front (`site/index.html`, constante `ALERT_ENDPOINT`) et crée le contact
dans EmailOctopus. Il existe uniquement pour que la clé API EmailOctopus ne soit
jamais servie dans le HTML statique.

Le double opt-in est géré par EmailOctopus (activé au niveau de la liste), pas ici.

## Prérequis côté EmailOctopus

- Liste « Alertes sudtechjobs » avec **double opt-in activé**.
- Deux champs personnalisés sur la liste, de type texte. Leur **merge tag**
  (onglet Fields -> colonne « Merge tag », sans les `{{ }}`) doit être reporté
  dans `wrangler.toml` : `EO_FIELD_SEARCH` et `EO_FIELD_LABEL`. Chez nous ce
  sont `Search` et `Searchlabel` (EmailOctopus capitalise et retire les `_`).

## Déploiement (une fois)

```bash
npm i -g wrangler
cd jobboard/worker
wrangler login
wrangler secret put EO_API_KEY      # colle la clé API EmailOctopus
wrangler secret put EO_LIST_ID      # 05a936ca-ad64-11f1-be12-877b53571838
wrangler deploy
```

`wrangler deploy` affiche l'URL publique, du type
`https://sudtechjobs-alerts.<sous-domaine>.workers.dev`.

Reporte-la dans `site/index.html` :

```js
const ALERT_ENDPOINT = "https://sudtechjobs-alerts.<sous-domaine>.workers.dev/subscribe";
```

## Test

```bash
curl -i -X POST https://sudtechjobs-alerts.<sous-domaine>.workers.dev/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"email":"toi@exemple.fr","search":"cat=eng&city=Nice","search_label":"Dev · Nice"}'
```

- `{"ok":true}` + email de confirmation EmailOctopus reçu → OK.
- `{"error":"provider","status":...,"detail":"..."}` → EmailOctopus a refusé
  l'appel ; colle la réponse, on ajuste le format (nom des champs, statut…).

## Redéployer après modif

```bash
cd jobboard/worker && wrangler deploy
```
