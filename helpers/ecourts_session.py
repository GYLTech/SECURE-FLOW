import os
import re
import threading
import time

BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"
COMPONENTS_URL = BASE_URL + "js/components.js"
PROBE_URL = BASE_URL + "?p=casestatus/fillDistrict"

GATE_VALUE_RE = re.compile(r'var\s+delimeter\s*=\s*"([^"]+)"')
GATE_NAME_RE = re.compile(r'"([A-Za-z0-9]{6,})"\s*:\s*delimeter')

BASE_AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://services.ecourts.gov.in",
    "Referer": BASE_URL + "?p=casestatus/index",
}

BLOCK_MARKERS = ("Search Page not Found here", "_event_transid")
REJECT_MARKER = "Invalid Request"

GATE_TTL_SECONDS = int(os.getenv("ECOURTS_GATE_TTL", "600"))
MAX_DISCOVERY_FETCHES = int(os.getenv("ECOURTS_GATE_MAX_FETCHES", "12"))

BREAKER_THRESHOLD = int(os.getenv("ECOURTS_BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN_SECONDS = int(os.getenv("ECOURTS_BREAKER_COOLDOWN", "900"))


class EcourtsGateError(Exception):
    pass


class EcourtsBlockedError(Exception):
    pass


def looks_blocked(response):
    return response.status_code == 405 or any(
        marker in response.text for marker in BLOCK_MARKERS
    )


def gate_rejected(response):
    return REJECT_MARKER in response.text


class _CircuitBreaker:
    def __init__(self, threshold, cooldown):
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def check(self):
        with self._lock:
            remaining = self._open_until - time.monotonic()
            if remaining > 0:
                raise EcourtsBlockedError(
                    f"eCourts circuit breaker is open for another "
                    f"{int(remaining)}s after {self._threshold} consecutive "
                    f"blocks. Requests are being shed so the IP can cool off."
                )

    def record_block(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown
                self._failures = 0
                print(
                    f"[breaker] eCourts blocked us {self._threshold}x - "
                    f"shedding requests for {self._cooldown}s"
                )

    def record_success(self):
        with self._lock:
            self._failures = 0

    def status(self):
        with self._lock:
            remaining = max(0, int(self._open_until - time.monotonic()))
            return {"open": remaining > 0, "cooldown_remaining": remaining,
                    "consecutive_blocks": self._failures}


breaker = _CircuitBreaker(BREAKER_THRESHOLD, BREAKER_COOLDOWN_SECONDS)


class _GateCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.alias = None
        self.resolved_at = 0.0

    def fresh(self):
        return (
            self.value
            and self.alias
            and (time.monotonic() - self.resolved_at) < GATE_TTL_SECONDS
        )

    def headers(self):
        headers = dict(BASE_AJAX_HEADERS)
        headers["delimeter"] = self.value
        headers[self.alias] = self.value
        return headers

    def store(self, value, alias):
        self.value = value
        self.alias = alias
        self.resolved_at = time.monotonic()

    def clear(self):
        self.value = None
        self.alias = None
        self.resolved_at = 0.0


_cache = _GateCache()


def _candidate_headers(value, alias):
    headers = dict(BASE_AJAX_HEADERS)
    headers["delimeter"] = value
    headers[alias] = value
    return headers


def _probe(session, headers, app_token):
    # Always probe with the session's live token: a previous probe in this same
    # discovery loop will have rotated it.
    token = getattr(session, "_app_token", "") or app_token

    try:
        response = session.post(
            PROBE_URL,
            data={"state_code": "1", "ajax_req": "true", "app_token": token},
            headers=headers,
            timeout=(10, 60),
        )
    except Exception as exc:
        print(f"[gate] probe error: {exc}")
        return False

    # eCourts rotates app_token on every response, this probe included. Dropping
    # the rotation leaves the caller posting a token the server has retired,
    # which comes back as "Invalid Request" on the very next call.
    try:
        rotated = response.json().get("app_token")
        if rotated:
            session._app_token = rotated
    except Exception:
        pass

    if looks_blocked(response):
        raise EcourtsBlockedError(
            "eCourts is refusing requests from this IP (HTTP 405 throttle "
            "stub). Retry later, or route through another IP via ECOURTS_PROXY."
        )

    if response.status_code != 200 or gate_rejected(response):
        return False

    return "<option" in response.text or "dist_code" in response.text


def _discover(session, app_token):
    tried = set()

    for attempt in range(1, MAX_DISCOVERY_FETCHES + 1):
        response = session.get(COMPONENTS_URL, timeout=(10, 60))

        if looks_blocked(response):
            raise EcourtsBlockedError(
                "eCourts is refusing requests from this IP while reading "
                "components.js. Retry later or use ECOURTS_PROXY."
            )

        value_match = GATE_VALUE_RE.search(response.text)
        aliases = [n for n in GATE_NAME_RE.findall(response.text) if n != "delimeter"]

        if not value_match or not aliases:
            raise EcourtsGateError(
                "Could not read the delimeter pair out of components.js - "
                "eCourts has most likely changed the gate again."
            )

        value, alias = value_match.group(1), aliases[0]
        if (value, alias) in tried:
            continue
        tried.add((value, alias))

        if _probe(session, _candidate_headers(value, alias), app_token):
            print(f"[gate] live delimeter found on fetch {attempt} (alias {alias})")
            return value, alias

        print(f"[gate] decoy delimeter rejected on fetch {attempt}")

    raise EcourtsGateError(
        f"eCourts delimeter unresolved after {MAX_DISCOVERY_FETCHES} fetches - "
        f"tried {len(tried)} distinct value(s). Either the decoy pool grew or "
        f"this IP is being served decoys only."
    )


def resolve_gate(session, app_token, force_refresh=False):
    breaker.check()

    if not force_refresh and _cache.fresh():
        return _cache.headers()

    with _cache.lock:
        if not force_refresh and _cache.fresh():
            return _cache.headers()
        if force_refresh:
            _cache.clear()

        try:
            value, alias = _discover(session, app_token)
        except EcourtsBlockedError:
            breaker.record_block()
            raise

        _cache.store(value, alias)
        breaker.record_success()
        return _cache.headers()


def invalidate_gate():
    with _cache.lock:
        _cache.clear()
