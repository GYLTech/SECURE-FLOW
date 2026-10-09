from http.client import RemoteDisconnected
import os
import random
import requests
import time

# Pause before each eCourts call so we don't look like a bot (seconds, jittered)
DELAY_MIN = float(os.getenv("ECOURTS_DELAY_MIN", "0.6"))
DELAY_MAX = float(os.getenv("ECOURTS_DELAY_MAX", "1.2"))
# (connect, read) — eCourts either answers within ~30s or not at all; waiting
# minutes only pushes the failure past the backend/ingress timeouts
TIMEOUT = (
    float(os.getenv("ECOURTS_CONNECT_TIMEOUT", "10")),
    float(os.getenv("ECOURTS_READ_TIMEOUT", "45")),
)

TRANSIENT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    RemoteDisconnected,
)


def polite_pause(attempt=0):
    """Short jittered pause; grows exponentially on retries (attempt 0 = first try)."""
    backoff = min(2 ** attempt - 1, 8) if attempt else 0
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX) + backoff)


def _request(method, session, url, max_retries, **kwargs):
    # Retry on the SAME session: eCourts binds app_token/captcha to its cookie,
    # so a fresh Session can never succeed. close() only drops pooled
    # connections; the session (cookies, headers, proxies) stays usable.
    last_error = None
    for attempt in range(max_retries):
        polite_pause(attempt)
        try:
            return session.request(method, url, timeout=TIMEOUT, **kwargs)
        except TRANSIENT_ERRORS as exc:
            last_error = exc
            print(f"[warn] eCourts {method} {url} failed (attempt {attempt + 1}/{max_retries}): "
                  f"{type(exc).__name__}")
            session.close()

    # A requests error (not a bare Exception) so callers can tell "eCourts is
    # unreachable" apart from a bug and answer with a 503
    raise requests.exceptions.ConnectionError(
        f"eCourts did not respond after {max_retries} attempts: {last_error!r}"
    )


def safe_get(session, url, params=None, max_retries=4, headers=None):
    return _request("GET", session, url, max_retries, params=params, headers=headers)


def safe_post(session, url, data, headers, max_retries=4):
    return _request("POST", session, url, max_retries, data=data, headers=headers)
