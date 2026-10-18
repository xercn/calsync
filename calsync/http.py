"""HTTP requests with backoff and retry (shared by Graph and Google)."""
import time

import requests

UA = "calsync/1.0"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status, text, url):
        super().__init__(f"HTTP {status} {url.split('?')[0]}: {text[:300]}")
        self.status = status
        self.text = text


def _is_rate_limited_403(resp):
    if resp.status_code != 403:
        return False
    try:
        errors = resp.json().get("error", {}).get("errors", [])
    except ValueError:
        return False
    return any(e.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded") for e in errors)


def request(session, method, url, *, ok=(200,), tries=6, sleep=time.sleep, **kw):
    """Return the Response; raise HttpError if the status code is not in ok.
    429 / 5xx / Google rate-limit 403 are retried with backoff."""
    kw.setdefault("timeout", 60)
    headers = kw.pop("headers", None) or {}
    headers.setdefault("User-Agent", UA)
    delay = 2
    for attempt in range(tries):
        try:
            resp = session.request(method, url, headers=headers, **kw)
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise HttpError(0, str(e), url)
            sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code in ok:
            return resp
        if (resp.status_code in RETRY_STATUS or _is_rate_limited_403(resp)) and attempt < tries - 1:
            retry_after = resp.headers.get("Retry-After", "")
            sleep(int(retry_after) if retry_after.isdigit() else delay)
            delay = min(delay * 2, 60)
            continue
        raise HttpError(resp.status_code, resp.text, url)
    raise AssertionError("unreachable")
