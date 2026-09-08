"""Shared helpers for the layer-2 institutional-directory scrapers.

Every ``annuaire/*.py`` script turns a cluster / French Tech member directory
into rows shaped like ``companies.json`` (``{name, domain?, source, ...}``) so
``merge_companies.py`` can concatenate them straight into the ATS resolver's
input.

stdlib only. A realistic browser UA + ``Accept`` is enough for every directory
we target today (they 403 a bare ``curl`` but not a browser-looking client).
If one starts returning 403 anyway, fetch it once through a headless browser
and drop the HTML/JSON next to the script.
"""
import gzip
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

_SOCIAL_RX = re.compile(
    r"(facebook\.|fb\.com|twitter\.|x\.com|linkedin\.|instagram\.|youtube\.|"
    r"youtu\.be|tiktok\.|pinterest\.|vimeo\.|t\.me|wa\.me|whatsapp\.|"
    r"google\.|goo\.gl|bit\.ly|maps\.|apple\.com|schema\.org|gmpg\.org|w\.org|"
    r"wordpress\.|gstatic\.|gravatar\.|cookiedatabase\.org)", re.I)


def get(url, timeout=30, retries=2, sleep=1.5):
    """GET a URL, return decoded text. Retries on 429/5xx and transient errors."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(sleep * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(sleep * (attempt + 1))
                continue
            raise
    raise last  # pragma: no cover


def get_json(url, **kw):
    return json.loads(get(url, **kw))


def norm_domain(value):
    """'https://www.Foo.com/careers' -> 'foo.com'.  Junk -> None."""
    if not value:
        return None
    d = str(value).strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0].strip()
    d = re.sub(r"^www\.", "", d).strip(". ")
    d = d.split("@")[-1]                      # in case an e-mail slipped through
    if not d or "." not in d:
        return None
    tld = d.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[a-z]{2,24}", tld):  # 'christophe.muhl' etc.
        return None
    return d


def is_social(url):
    return bool(_SOCIAL_RX.search(url or ""))


def clean_text(html_fragment):
    """Strip tags + collapse whitespace + unescape entities."""
    from html import unescape
    t = re.sub(r"<[^>]+>", " ", html_fragment or "")
    return re.sub(r"\s+", " ", unescape(t)).strip()


def slugify(name):
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")


def dedupe(rows):
    """Drop rows whose (domain) or (normalized name) was already seen."""
    seen_dom, seen_name, out = set(), set(), []
    for r in rows:
        d = r.get("domain")
        nk = re.sub(r"[^a-z0-9]+", "", (r.get("name") or "").lower())
        if (d and d in seen_dom) or (nk and nk in seen_name):
            continue
        if d:
            seen_dom.add(d)
        if nk:
            seen_name.add(nk)
        out.append(r)
    return out


def write(path, rows, label):
    import os
    import sys
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    got = sum(1 for r in rows if r.get("domain"))
    print("wrote %s  (%d %s, %d with a domain)" % (path, len(rows), label, got),
          file=sys.stderr)
