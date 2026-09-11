#!/usr/bin/env bash
# Full refresh of the job board feed. Safe to run locally or in CI.
#   ./jobboard/pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "::group::WTTJ browse (layer 1)"
python3 jobboard/sources/wttj.py
echo "::endgroup::"

echo "::group::WTTJ enrich (layer 1b)"
python3 jobboard/sources/wttj_enrich.py --workers 10
echo "::endgroup::"

echo "::group::France Travail (layer 1c, best effort — needs FT_CLIENT_ID/SECRET)"
if [ -n "${FT_CLIENT_ID:-}" ] || [ -f jobboard/.env ]; then
  python3 jobboard/sources/francetravail.py \
    || echo "France Travail fetch failed — skipping layer 1c"
else
  echo "no France Travail credentials — skipping layer 1c"
fi
echo "::endgroup::"

echo "::group::ATS fetch (layer 3, best effort)"
python3 jobboard/fetch_jobs.py -o jobboard/data/ats_jobs.json \
  || echo "ATS fetch failed — building with WTTJ only"
echo "::endgroup::"

echo "::group::merge + build feed"
python3 jobboard/build.py
echo "::endgroup::"

echo "::group::company profiles (layer 4)"
python3 jobboard/build_companies.py
echo "::endgroup::"

echo "::group::render static SEO pages (layer 5)"
python3 jobboard/render_pages.py
echo "::endgroup::"

echo "::group::email alerts (layer 6, best effort — needs EMAILOCTOPUS_*/RESEND_API_KEY)"
python3 jobboard/alerts.py || echo "alerts failed — continuing"
echo "::endgroup::"

# Layers not in the daily path (slow, rarely change) — run by hand when needed:
#   python3 jobboard/annuaire/frenchtech_amp.py         # French Tech Aix-Marseille
#   python3 jobboard/annuaire/frenchtech_cotedazur.py   # French Tech Côte d'Azur / Sophia-Nice
#   python3 jobboard/annuaire/telecom_valley.py         # cluster Telecom Valley
#   python3 jobboard/annuaire/aktantis.py               # Aktantis / ex-Pôle SCS (deeptech PACA)
#   python3 jobboard/annuaire/medinsoft.py              # Medinsoft (Marseille/Aix)
#   python3 jobboard/merge_companies.py                 # curated + annuaires -> companies.all.json
#   python3 jobboard/resolve.py -i jobboard/data/companies.all.json \
#           -o jobboard/companies.resolved.json --workers 20   # re-detect ATS per company
