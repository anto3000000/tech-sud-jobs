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

echo "::group::ATS fetch (layer 3, best effort)"
python3 jobboard/fetch_jobs.py -o jobboard/data/ats_jobs.json \
  || echo "ATS fetch failed — building with WTTJ only"
echo "::endgroup::"

echo "::group::merge + build feed"
python3 jobboard/build.py
echo "::endgroup::"

echo "::group::render static SEO pages (layer 5)"
python3 jobboard/render_pages.py
echo "::endgroup::"

# Layers not in the daily path (slow, rarely change) — run by hand when needed:
#   python3 jobboard/annuaire/frenchtech_amp.py     # refresh company directory
#   python3 jobboard/resolve.py                     # re-detect ATS per company
