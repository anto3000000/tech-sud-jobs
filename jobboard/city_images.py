#!/usr/bin/env python3
"""Layer 4.6 — a real photo for each city facet's hero banner.

Wikipedia's MediaWiki API (`action=query&prop=pageimages`) returns the lead
photo of a city's article, already scaled to a sane width server-side (asking
for an arbitrary width via the raw thumb.wikimedia.org URL 400s unless that
exact width happens to be pre-rendered — the pageimages API picks a working
size for us). Results are cached in data/city_images.json, keyed by city name,
so a normal run only fetches cities that are new or whose entry has expired —
one batched request covers up to 50 cities at once.

    python3 jobboard/city_images.py
    python3 jobboard/city_images.py --force        # ignore the cache entirely

Some articles have no usable photo (a logo, an icon, or nothing at all) — those
are cached as a miss too (so we don't refetch them every run) and render_pages
falls back to the designed gradient/skyline banner for that city.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from jobboard.render_pages import slugify, CITY_DEPT, PACA_DEPTS  # noqa: E402

DATA = os.path.join(HERE, "data")
SITE = os.path.join(HERE, "site")
FEED = os.path.join(SITE, "jobs.json")
OUT = os.path.join(DATA, "city_images.json")

API = "https://fr.wikipedia.org/w/api.php"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 sudtechjobs-bot/1.0 "
      "(hello@sudtechjobs.com)")
BATCH = 45
MAX_AGE_DAYS = 60
THUMB_WIDTH = 1200

# a lead image whose filename contains one of these is a logo/diagram/map, not
# a photo worth using as a banner
BAD_FILE_RX = re.compile(r"(logo|blason|armoiries|map|carte|drapeau|flag)", re.I)

# hand-picked for towns where the automated lookup finds a technically-real
# but weak/wrong photo (a highway exit sign, a homonymous town...) — checked
# before the API calls, so these never get overwritten by a worse auto-match
MANUAL_OVERRIDES = {
    "Sophia Antipolis": {
        "file": "Sophia Antipolis.jpg",
        "url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/d/de/Sophia_Antipolis.jpg/1280px-Sophia_Antipolis.jpg",
        "width": 1200, "height": 675,
        "commons_url": "https://commons.wikimedia.org/wiki/File:Sophia_Antipolis.jpg",
    },
}


def _get(params, timeout=20):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _cities_from_feed():
    feed = json.load(open(FEED, encoding="utf-8"))
    return sorted({j.get("city") for j in feed.get("jobs", [])
                   if j.get("city") and j["city"] != "Remote"})


def fetch_batch(cities):
    """titles=... may follow redirects (e.g. a typo'd name) — `redirects=1`
    folds those into the canonical title, which we map back to every alias
    that pointed at it."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "prop": "pageimages", "piprop": "thumbnail|name", "pithumbsize": str(THUMB_WIDTH),
        "redirects": "1", "titles": "|".join(cities),
    }
    data = _get(params)
    query = data.get("query", {})
    alias_of = {r["from"]: r["to"] for r in query.get("redirects", [])}
    by_title = {p.get("title"): p for p in query.get("pages", [])}

    out = {}
    for city in cities:
        page = by_title.get(alias_of.get(city, city))
        if not page:
            out[city] = None
            continue
        thumb = page.get("thumbnail")
        fname = page.get("pageimage") or ""
        if not thumb or not thumb.get("source") or BAD_FILE_RX.search(fname):
            out[city] = None
            continue
        out[city] = {
            "url": thumb["source"],
            "width": thumb.get("width"), "height": thumb.get("height"),
            "file": fname,
            "commons_url": "https://commons.wikimedia.org/wiki/File:%s"
                           % urllib.parse.quote(fname.replace(" ", "_")),
        }
    return out


def commons_fallback(city, limit=6):
    """Second try for a city whose own Wikipedia article has no usable lead
    photo (a logo, or no image at all — Sophia Antipolis, Biot, Vitrolles...):
    search Commons directly and take the first landscape-oriented photo.

    Gated to names we already trust as real PACA towns (CITY_DEPT, the same
    map render_pages uses for département roll-ups) — a bare full-text search
    on a raw ATS location string ("83190", "Marseille Area", "Orange") is as
    likely to return a mineral sample, a painting or a citrus fruit as a town.
    The département name is appended to the query to break homonyms (there
    are two French communes named Vitrolles; "Orange" alone finds the fruit).
    """
    dept = PACA_DEPTS.get(CITY_DEPT.get(slugify(city), ""))
    if not dept:
        return None
    query = "%s %s" % (city, dept[1])
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|size", "iiurlwidth": str(THUMB_WIDTH),
    }
    try:
        resp = _commons_get(params)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return None
    for page in resp.get("query", {}).get("pages", []):
        fname = page.get("title", "").split(":", 1)[-1]
        # a free-text Commons search surfaces maps, diagrams and paintings as
        # readily as photos; unlike the direct pageimages lookup above, this
        # tier gets stricter filters — real photos are overwhelmingly JPEG,
        # maps/diagrams/scans lean PNG/TIFF/GIF (BAD_FILE_RX alone missed a
        # German-labelled administrative map for Sophia Antipolis)
        if BAD_FILE_RX.search(fname) or not re.search(r"\.jpe?g$", fname, re.I):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        url, w, h = info.get("thumburl"), info.get("thumbwidth"), info.get("thumbheight")
        if not url or not w or not h or w < h:  # skip portrait shots — bad fit for a wide banner
            continue
        return {
            "url": url, "width": w, "height": h, "file": fname,
            "commons_url": info.get("descriptionurl")
            or ("https://commons.wikimedia.org/wiki/File:%s" % urllib.parse.quote(fname.replace(" ", "_"))),
        }
    return None


def _commons_get(params, timeout=20):
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="ignore the cache, refetch every city")
    args = ap.parse_args()

    if not os.path.exists(FEED):
        sys.exit("no %s — run jobboard/build.py first" % FEED)
    cities = _cities_from_feed()

    try:
        cache = {} if args.force else json.load(open(OUT, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    now = time.time()
    for city in cities:
        if city in MANUAL_OVERRIDES:
            cache[city] = dict(MANUAL_OVERRIDES[city], checked_at=now)
    stale = [c for c in cities if c not in MANUAL_OVERRIDES and (
        c not in cache or now - cache[c].get("checked_at", 0) > MAX_AGE_DAYS * 86400)]

    fetched = hits = 0
    for i in range(0, len(stale), BATCH):
        batch = stale[i:i + BATCH]
        try:
            results = fetch_batch(batch)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            print("  city_images: batch fetch failed (%s) — keeping stale/missing entries"
                  % e, file=sys.stderr)
            continue
        for city, image in results.items():
            if not image:
                try:
                    image = commons_fallback(city)
                except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
                    image = None
                time.sleep(0.15)
            cache[city] = dict(image, checked_at=now) if image else {"checked_at": now}
            fetched += 1
            hits += bool(image)
        time.sleep(0.2)

    # drop cities that fell out of the feed entirely, keep the file bounded
    for city in list(cache):
        if city not in cities:
            del cache[city]

    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)

    with_photo = sum(1 for v in cache.values() if v.get("url"))
    print("city_images: %d cities tracked, %d with a photo (%d fetched this run, %d hits)"
          % (len(cache), with_photo, fetched, hits), file=sys.stderr)


if __name__ == "__main__":
    main()
