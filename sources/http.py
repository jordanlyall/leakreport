"""Small HTTP layer: token bucket, Retry-After, backoff on 429/5xx. Transport is injectable for tests.

A transport is `fn(method, url, headers, body) -> (status, headers_dict, body_bytes)`.
"""
import json
import math
import time
import urllib.error
import urllib.request

USER_AGENT = "leakreport/0.1 (read-only wallet report)"


def urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None, clock=time.monotonic, sleep=time.sleep):
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()

    def take(self):
        while True:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            self.sleep((1 - self.tokens) / self.rate)


class HttpError(RuntimeError):
    pass


class Http:
    def __init__(self, transport=urllib_transport, bucket: TokenBucket | None = None, sleep=time.sleep,
                 max_retries: int = 6, headers: dict | None = None):
        self.transport = transport
        self.bucket = bucket
        self.sleep = sleep
        self.max_retries = max_retries
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        self.calls = 0

    def request(self, method: str, url: str, body: dict | None = None, redact: str | None = None):
        """Returns parsed JSON. `redact` is a safe label used in error messages instead of the URL."""
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        label = redact or url
        for attempt in range(self.max_retries + 1):
            if self.bucket:
                self.bucket.take()
            self.calls += 1
            try:
                status, rh, raw = self.transport(method, url, headers, data)
            except (urllib.error.URLError, OSError) as e:   # timeouts, resets; message may echo the URL
                if attempt == self.max_retries:
                    raise HttpError(f"{label}: network error ({type(e).__name__}) after {attempt + 1} attempts") from None
                self.sleep(_retry_after({}, attempt))
                continue
            if status == 429 or status >= 500:
                if attempt == self.max_retries:
                    raise HttpError(f"{label}: HTTP {status} after {attempt + 1} attempts")
                self.sleep(_retry_after(rh, attempt))
                continue
            if status >= 400:
                raise HttpError(f"{label}: HTTP {status}: {raw[:200]!r}")
            if rh.get("x-ratelimit-remaining") == "0" and rh.get("x-ratelimit-reset"):
                try:
                    self.sleep(min(60.0, max(0.0, float(rh["x-ratelimit-reset"]))))
                except ValueError:
                    pass
            return json.loads(raw) if raw else None
        raise HttpError(f"{label}: retries exhausted")

    def get(self, url: str, redact: str | None = None):
        return self.request("GET", url, redact=redact)


def _retry_after(headers: dict, attempt: int) -> float:
    ra = headers.get("retry-after")
    if ra is not None:
        try:
            v = float(ra)
            if math.isfinite(v):
                return min(60.0, max(0.0, v))
        except ValueError:
            pass
    return min(60.0, 2.0 ** attempt)
