"""Tiny stdlib-only HTTP helpers (no third-party deps, runs on system python3.9)."""
import gzip
import json
import time
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20


class HttpResult:
    __slots__ = ("status", "url", "headers", "body", "error")

    def __init__(self, status=None, url=None, headers=None, body="", error=None):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.body = body
        self.error = error

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 300

    def json(self):
        return json.loads(self.body)


def _read(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", "replace")


def request(url, method="GET", data=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=1):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    payload = None
    if data is not None:
        payload = json.dumps(data).encode() if not isinstance(data, (bytes, bytearray)) else data
        hdrs.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResult(resp.status, resp.geturl(), dict(resp.headers), _read(resp))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = _read(e)
            except Exception:
                pass
            last = HttpResult(e.code, url, dict(e.headers or {}), body, error="HTTPError")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return last
        except Exception as e:  # noqa: BLE001 - network layer, report don't crash
            last = HttpResult(None, url, {}, "", error="%s: %s" % (type(e).__name__, e))
            if attempt < retries:
                time.sleep(1.0)
                continue
            return last
    return last


def get_text(url, **kw):
    return request(url, method="GET", **kw)


def get_json(url, **kw):
    r = request(url, method="GET", **kw)
    return r


def post_json(url, data, **kw):
    return request(url, method="POST", data=data, **kw)
