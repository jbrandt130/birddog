# (c) 2025 Jonathan Brandt
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import logging
import random
import re
import threading
import time
import requests
from requests.adapters import HTTPAdapter
from dataclasses import dataclass, field
from typing import Dict, Optional, Mapping, Tuple
from urllib.parse import urlparse

from birddog.log import get_logger
_logger = get_logger()

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.monotonic()


def _ci_get(headers: Optional[Mapping[str, str]], key: str) -> Optional[str]:
    if not headers:
        return None
    lk = key.lower()
    for k, v in headers.items():
        if k.lower() == lk:
            return v
    return None


def _parse_retry_after(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    ra = _ci_get(headers, "Retry-After")
    if not ra:
        return None
    ra = ra.strip()
    try:
        return max(0.0, float(int(ra)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# configuration objects
# ---------------------------------------------------------------------------

# HostProfile parameter reference
#
# initial_rps
#   Starting request rate (requests per second) when the host is first seen.
#   Should be conservative; adaptive logic will ramp up if safe.
#
# min_rps
#   Lower bound on request rate after backoff.
#   Prevents rate from collapsing to zero during repeated errors.
#
# max_rps
#   Upper bound on request rate, regardless of observed success.
#   Acts as a safety cap even if no throttling signals are received.
#
# bucket_capacity
#   Token bucket capacity (number of requests).
#   Allows short bursts above steady-state rate.
#   Effective burst window ≈ bucket_capacity / rate_rps seconds.
#
# ai_step
#   Additive increase step (requests per second).
#   Applied only when the host has been stable and no recent throttling occurred.
#
# increase_every_s
#   Minimum time (seconds) between additive rate increases.
#   Larger values reduce oscillation but slow convergence.
#
# md_429
#   Multiplicative decrease factor applied on definitive rate-limit signals
#   (HTTP 429 or equivalent payload-level throttling).
#   Typical range: 0.5–0.7.
#
# md_transient
#   Multiplicative decrease factor applied on transient failures
#   (5xx responses, timeouts, connection resets).
#   Should be mild (e.g., 0.85–0.95).
#
# transient_cooldown_s
#   Tuple (min_seconds, max_seconds) defining a randomized cooldown
#   applied after a transient error.
#   Adds jitter and prevents immediate retry bursts.
#
# burst_error_window_s
#   Time window (seconds) for counting transient errors when detecting bursts.
#   Defaults to 30 seconds.
#
# burst_error_threshold
#   Number of transient errors within burst_error_window_s that constitutes a burst.
#   When exceeded, stronger backoff rules are applied.
#
# burst_md
#   Multiplicative decrease factor applied when a transient error burst is detected.
#   Typically stronger than md_transient (e.g., 0.6–0.8).
#
# burst_cooldown_s
#   Tuple (min_seconds, max_seconds) defining cooldown duration when a burst
#   of transient errors is detected.
#
# max_in_flight
#   Maximum number of concurrent in-flight requests allowed for this host.
#   Implemented via a semaphore.
#   This caps concurrency independently of rate (RPS).

@dataclass(frozen=True)
class HostProfile:
    # rate control
    initial_rps: float
    min_rps: float
    max_rps: float
    bucket_capacity: float

    # adaptive tuning
    ai_step: float
    increase_every_s: float
    md_429: float
    md_transient: float

    # cooldowns
    transient_cooldown_s: Tuple[float, float]

    # burst handling
    burst_error_window_s: float = 30.0
    burst_error_threshold: int = 3
    burst_md: float = 0.7
    burst_cooldown_s: Tuple[float, float] = (1.0, 3.0)

    # concurrency
    max_in_flight: int = 4

    # bypass all throttling (rate + concurrency) for this host
    unthrottled: bool = False

# HostState field reference
#
# profile
#   The HostProfile associated with this host key.
#   Defines all static tuning parameters and limits for the host.
#
# rate_rps
#   Current adaptive request rate (requests per second).
#   Initialized from profile.initial_rps and adjusted dynamically
#   via additive increase and multiplicative decrease.
#
# tokens
#   Current number of available tokens in the token bucket.
#   Decremented on each acquire; replenished over time at rate_rps.
#
# last_ts
#   Monotonic timestamp of the last token bucket refill calculation.
#   Used to compute how many tokens to add based on elapsed time.
#
# blocked_until
#   Monotonic timestamp until which requests are fully blocked.
#   Set on Retry-After, throttling, or cooldown events.
#
# last_increase_ts
#   Monotonic timestamp of the last successful additive rate increase.
#   Prevents increasing rate too frequently.
#
# ok_since_last_increase
#   Count of successful (non-error, non-throttle) responses
#   since the last additive rate increase attempt.
#   Used to ensure increases only occur when traffic is flowing successfully.
#
# transient_err_times
#   List of monotonic timestamps for recent transient errors
#   (5xx responses, timeouts, connection resets).
#   Pruned to burst_error_window_s and used to detect error bursts.
#
# sem
#   Semaphore controlling maximum concurrent in-flight requests
#   for this host.
#   Acquired before rate limiting logic to prevent thread pileups.


@dataclass
class HostState:
    profile: HostProfile
    rate_rps: float
    tokens: float
    last_ts: float = field(default_factory=_now)

    blocked_until: float = 0.0

    last_increase_ts: float = 0.0
    ok_since_last_increase: int = 0

    transient_err_times: list[float] = field(default_factory=list)
    sem: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))

    # actual throughput tracking
    completed: int = 0
    completed_since: float = field(default_factory=_now)


# ---------------------------------------------------------------------------
# lease
# ---------------------------------------------------------------------------

class Lease:
    __slots__ = ("_throttle", "_host_key", "_released", "_has_sem")

    def __init__(self, throttle: "AdaptiveThrottle", host_key: str, *, has_sem: bool = True):
        self._throttle = throttle
        self._host_key = host_key
        self._released = False
        self._has_sem = has_sem

    def release(self) -> None:
        if not self._released:
            self._released = True
            if self._has_sem:
                self._throttle._release(self._host_key)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# ---------------------------------------------------------------------------
# resolver (INVERTED ALIAS SEMANTICS)
# ---------------------------------------------------------------------------

class HostKeyResolver:
    """
    Resolves URL or host_key to a canonical profile key.

    alias_rules:
        { profile_key : regex }

    If regex matches the URL or hostname, profile_key is used.
    """

    def __init__(self, *, alias_rules: Optional[Dict[str, str]] = None):
        self._rules: list[tuple[str, re.Pattern]] = []
        for profile_key, pattern in (alias_rules or {}).items():
            self._rules.append((profile_key.lower(), re.compile(pattern, re.I)))

    def key_from(self, url_or_key: str) -> str:
        s = (url_or_key or "").strip()

        # First: regex-based aliasing
        for profile_key, rx in self._rules:
            if rx.search(s):
                return profile_key

        # If URL → derive host-based key
        if "://" in s:
            u = urlparse(s)
            host = (u.hostname or "").lower()
            if host:
                return f"{host}:api"

        # Not a URL: normalize bare hostname
        s = s.lower()
        if ":" not in s and "." in s:
            return f"{s}:api"

        return s


# ---------------------------------------------------------------------------
# throttle
# ---------------------------------------------------------------------------

class AdaptiveThrottle:
    """
    Thread-safe per-host adaptive throttle.
    """

    def __init__(
        self,
        profiles: Dict[str, HostProfile],
        *,
        default_profile: Optional[HostProfile] = None,
        resolver: Optional[HostKeyResolver] = None,
        logger: Optional[logging.Logger] = None,
        sleep_jitter: float = 0.10,
    ):
        self._profiles = {k.lower(): v for k, v in profiles.items()}
        self._default_profile = default_profile
        self._resolver = resolver or HostKeyResolver()
        self._log = logger or logging.getLogger(__name__)
        self._sleep_jitter = max(0.0, float(sleep_jitter))

        self._lock = threading.RLock()
        self._hosts: Dict[str, HostState] = {}
        self._warned_unknown: set[str] = set()

    def key_from(self, url_or_key: str) -> str:
        return self._resolver.key_from(url_or_key)

    def _get_profile(self, host_key: str) -> HostProfile:
        hk = host_key.lower()
        prof = self._profiles.get(hk)
        if prof:
            return prof

        if not self._default_profile:
            raise KeyError(f"No profile for {host_key!r} and no default_profile set")

        if hk not in self._warned_unknown:
            self._warned_unknown.add(hk)
            self._log.warning(
                "AdaptiveThrottle: unknown host_key %r; using default profile",
                host_key,
            )

        return self._default_profile

    def _get_state(self, host_key: str) -> HostState:
        st = self._hosts.get(host_key)
        if st is None:
            prof = self._get_profile(host_key)
            st = HostState(
                profile=prof,
                rate_rps=prof.initial_rps,
                tokens=prof.bucket_capacity,
                last_increase_ts=_now(),
                sem=threading.Semaphore(prof.max_in_flight),
            )
            self._hosts[host_key] = st
        return st

    def _refill(self, st: HostState, now: float) -> None:
        dt = now - st.last_ts
        if dt > 0:
            st.tokens = min(st.profile.bucket_capacity, st.tokens + dt * st.rate_rps)
            st.last_ts = now

    # ---------------------------------------------------------------------

    def acquire(self, url_or_key: str) -> Lease:
        host_key = self.key_from(url_or_key)

        with self._lock:
            st = self._get_state(host_key)
            if st.profile.unthrottled:
                return Lease(self, host_key, has_sem=False)
            sem = st.sem

        sem.acquire()

        try:
            while True:
                with self._lock:
                    now = _now()
                    st = self._get_state(host_key)

                    if st.blocked_until > now:
                        sleep_s = st.blocked_until - now
                    else:
                        self._refill(st, now)
                        if st.tokens >= 1.0:
                            st.tokens -= 1.0
                            return Lease(self, host_key)
                        sleep_s = (1.0 - st.tokens) / max(st.rate_rps, 1e-9)

                if sleep_s > 0:
                    if self._sleep_jitter:
                        sleep_s *= random.uniform(
                            1.0 - self._sleep_jitter,
                            1.0 + self._sleep_jitter,
                        )
                    time.sleep(max(0.0, sleep_s))
        except Exception:
            sem.release()
            raise

    def _release(self, host_key: str) -> None:
        with self._lock:
            st = self._hosts.get(host_key)
            if st:
                st.sem.release()

    # ---------------------------------------------------------------------
    # reporting
    # ---------------------------------------------------------------------

    def report_http(
        self,
        url_or_key: str,
        status_code: int,
        headers: Optional[Mapping[str, str]] = None,
        *,
        is_rate_limited_payload: bool = False,
    ) -> None:
        host_key = self.key_from(url_or_key)

        with self._lock:
            self._get_state(host_key).completed += 1

        if status_code == 429 or is_rate_limited_payload:
            self._on_throttle(host_key, headers)
        elif status_code in (500, 502, 503, 504, 408):
            self._on_transient(host_key)
        elif status_code in (401, 403):
            self._on_auth(host_key)
        elif 400 <= status_code < 500:
            self._on_client_error(host_key)
        else:
            self._on_success(host_key)

    def report_exception(self, url_or_key: str) -> None:
        self._on_transient(self.key_from(url_or_key))

    # ---------------------------------------------------------------------
    # internal adjustments
    # ---------------------------------------------------------------------

    def _on_throttle(self, host_key: str, headers: Optional[Mapping[str, str]]) -> None:
        with self._lock:
            st = self._get_state(host_key)
            prof = st.profile
            now = _now()

            st.rate_rps = max(prof.min_rps, st.rate_rps * prof.md_429)

            ra = _parse_retry_after(headers)
            if ra is None:
                ra = min(30.0, max(1.0, 1.0 / max(st.rate_rps, 1e-9)))

            st.blocked_until = max(
                st.blocked_until,
                now + ra * random.uniform(0.9, 1.1),
            )

            st.ok_since_last_increase = 0
            st.last_increase_ts = now
            st.transient_err_times.clear()

    def _on_transient(self, host_key: str) -> None:
        with self._lock:
            st = self._get_state(host_key)
            prof = st.profile
            now = _now()

            st.transient_err_times.append(now)
            cutoff = now - prof.burst_error_window_s
            st.transient_err_times[:] = [t for t in st.transient_err_times if t >= cutoff]

            burst = len(st.transient_err_times) >= prof.burst_error_threshold

            if burst:
                st.rate_rps = max(prof.min_rps, st.rate_rps * prof.burst_md)
                cd_lo, cd_hi = prof.burst_cooldown_s
            else:
                st.rate_rps = max(prof.min_rps, st.rate_rps * prof.md_transient)
                cd_lo, cd_hi = prof.transient_cooldown_s

            st.blocked_until = max(
                st.blocked_until,
                now + random.uniform(cd_lo, cd_hi),
            )

            st.ok_since_last_increase = 0
            st.last_increase_ts = now

    def _on_auth(self, host_key: str) -> None:
        with self._lock:
            self._get_state(host_key).ok_since_last_increase = 0

    def _on_client_error(self, host_key: str) -> None:
        with self._lock:
            self._get_state(host_key).ok_since_last_increase = 0

    def _on_success(self, host_key: str) -> None:
        with self._lock:
            st = self._get_state(host_key)
            prof = st.profile
            now = _now()

            st.ok_since_last_increase += 1
            if now - st.last_increase_ts >= prof.increase_every_s:
                st.rate_rps = min(prof.max_rps, st.rate_rps + prof.ai_step)
                st.ok_since_last_increase = 0
                st.last_increase_ts = now

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            now = _now()
            return {
                hk: {
                    "rate_rps": st.rate_rps,
                    "tokens": st.tokens,
                    "blocked_for_s": max(0.0, st.blocked_until - now),
                    "max_in_flight": st.profile.max_in_flight,
                }
                for hk, st in self._hosts.items()
            }

    def format_report(self) -> str:
        """
        Format a one-shot textual report of current host throttle state.

        Computes actual throughput (rps) since the last call to this method,
        then resets per-host counters. Thread-safe.
        """
        with self._lock:
            now = _now()
            if not self._hosts:
                return "service throttle report: (no hosts seen yet)"

            rows = []
            for hk in sorted(self._hosts.keys()):
                st = self._hosts[hk]
                elapsed = now - st.completed_since
                actual_rps = st.completed / max(elapsed, 1e-9)
                rows.append((
                    hk,
                    st.rate_rps,
                    actual_rps,
                    st.tokens,
                    max(0.0, st.blocked_until - now),
                    st.profile.max_in_flight,
                ))
                st.completed = 0
                st.completed_since = now

        lines = ["service throttle report:"]
        lines.append(
            "  {:<34} {:>8} {:>8} {:>8} {:>10} {:>12}".format(
                "host_key", "cfg_rps", "act_rps", "tokens", "blocked_s", "max_in_flight"
            )
        )
        lines.append("  " + "-" * 88)
        for hk, cfg_rps, act_rps, tokens, blocked_s, max_in_flight in rows:
            lines.append(
                "  {:<34} {:>8.2f} {:>8.2f} {:>8.2f} {:>10.2f} {:>12d}".format(
                    hk[:34], cfg_rps, act_rps, tokens, blocked_s, max_in_flight,
                )
            )
        return "\n".join(lines)

# ---------------------------------------------------------------------------
# profiles & resolver rules
# ---------------------------------------------------------------------------

PROFILES: Dict[str, HostProfile] = {
    "uk.wikisource.org:api": HostProfile(
        initial_rps=4.0, min_rps=0.2, max_rps=50.0, bucket_capacity=5.0,
        ai_step=0.25, increase_every_s=15.0,
        md_429=0.6, md_transient=0.9,
        transient_cooldown_s=(0.2, 0.6),
        max_in_flight=4,
    ),
    "commons.wikimedia.org:api": HostProfile(
        initial_rps=4.0, min_rps=0.2, max_rps=50.0, bucket_capacity=5.0,
        ai_step=0.25, increase_every_s=15.0,
        md_429=0.6, md_transient=0.9,
        transient_cooldown_s=(0.2, 0.6),
        max_in_flight=4,
    ),
    "translation.googleapis.com:api": HostProfile(
        initial_rps=2.0, min_rps=0.2, max_rps=20.0, bucket_capacity=5.0,
        ai_step=0.25, increase_every_s=20.0,
        md_429=0.6, md_transient=0.9,
        transient_cooldown_s=(0.2, 0.6),
        max_in_flight=8,
    ),
    "nocodb.internal:api": HostProfile(
        initial_rps=20.0, min_rps=1.0, max_rps=100.0, bucket_capacity=40.0,
        ai_step=1.0, increase_every_s=10.0,
        md_429=0.7, md_transient=0.85,
        transient_cooldown_s=(0.1, 0.3),
        max_in_flight=24,
    ),
    "localhost:api": HostProfile(
        # local NocoDB backed by SQLite in Docker -- SQLite serializes
        # writers, so a production-sized concurrency ceiling just causes
        # requests to queue behind each other past the read timeout
        initial_rps=5.0, min_rps=0.5, max_rps=20.0, bucket_capacity=8.0,
        ai_step=0.5, increase_every_s=15.0,
        md_429=0.7, md_transient=0.7,
        transient_cooldown_s=(0.3, 0.8),
        max_in_flight=4,
    ),
    # NocoDB Cloud (hosted on nocodb.com)
    # Docs indicate 5 req/s per user; when exceeded, 429 and "wait ~30s".
    # We stay under the limit to reduce oscillation, ramp slowly, and back off hard on 429.
    "nocodb.cloud:api": HostProfile(
        initial_rps=3.0, min_rps=0.2, max_rps=4.5, bucket_capacity=4.0,
        ai_step=0.25, increase_every_s=20.0,
        md_429=0.5, md_transient=0.9,
        transient_cooldown_s=(0.3, 0.9),
        burst_error_window_s=30.0, burst_error_threshold=3, burst_md=0.7, burst_cooldown_s=(2.0, 5.0),
        max_in_flight=4,
    ),
}

DEFAULT_PROFILE = HostProfile(
    initial_rps=1.0, min_rps=0.1, max_rps=20.0, bucket_capacity=3.0,
    ai_step=0.10, increase_every_s=20.0,
    md_429=0.6, md_transient=0.9,
    transient_cooldown_s=(0.5, 1.0),
    max_in_flight=2,
)

RESOLVER = HostKeyResolver(
    alias_rules={
        # EB hostnames containing "nocodb" somewhere in the host label(s)
        "nocodb.internal:api": r"(?i)(?:https?://)?[^/\s]*nocodb[^/\s]*\.elasticbeanstalk\.com\b",

        # NocoDB Cloud (nocodb.com) — match URL or bare hostname
        "nocodb.cloud:api": r"(?i)(?:https?://)?([a-z0-9-]+\.)*nocodb\.com(?:[:/]|$)",
    }
)

# ---------------------------------------------------------------------------
# global request throttle singleton
# ---------------------------------------------------------------------------

THROTTLE = AdaptiveThrottle(
    PROFILES,
    default_profile=DEFAULT_PROFILE,
    resolver=RESOLVER,
)

# ---------------------------------------------------------------------------
# session factory
# ---------------------------------------------------------------------------

def make_session(url_or_key: str) -> requests.Session:
    """
    Create a requests.Session whose connection pool is sized to match the
    max_in_flight concurrency allowed by the throttle profile for the given
    host.  Pass a URL or host key (e.g. ``"nocodb.internal:api"``).
    """
    host_key = THROTTLE.key_from(url_or_key)
    try:
        profile = THROTTLE._get_profile(host_key)
        pool_size = max(profile.max_in_flight, 10)
    except KeyError:
        pool_size = 10

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# ---------------------------------------------------------------------------
# page loading
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
BASE_BACKOFF = 1.0  # seconds
MAX_BACKOFF = 30.0  # max wait time in seconds
REQUEST_TIMEOUT = 10 # seconds

_url_headers = {
        'User-Agent': 'BirddogBot/1.0 (non-commercial research, contact: birddogpound@gmail.com)'
    }

class FetchUrlFailError(RuntimeError):
    """Raised when fetch_url fails after exhausting all retries."""
    pass

class _RateLimitedPayload(Exception):
    pass

class _TransientPayload(Exception):
    pass

# Retry only truly transient HTTP statuses (excluding 429; handled separately)
_TRANSIENT_HTTP = {408, 500, 502, 503, 504}

# MediaWiki “error.code” values worth special handling
_MW_RATE_LIMIT_CODES = {"ratelimited"}   # treat as 429
_MW_TRANSIENT_CODES = {"maxlag"}         # treat as transient/backoff

# reporting globals
_THROTTLE_REPORT_EVERY_S = 60.0
_throttle_last_report_ts = 0.0
_throttle_report_lock = threading.Lock()

def _maybe_log_throttle_report() -> None:
    global _throttle_last_report_ts
    now = _now()
    if now - _throttle_last_report_ts < _THROTTLE_REPORT_EVERY_S:
        return
    # only one thread logs, others skip
    if not _throttle_report_lock.acquire(blocking=False):
        return
    try:
        now = _now()
        if now - _throttle_last_report_ts < _THROTTLE_REPORT_EVERY_S:
            return
        _throttle_last_report_ts = now
        _logger.info("\n%s", THROTTLE.format_report())
    finally:
        _throttle_report_lock.release()


def _maybe_mediawiki_error_code(response: requests.Response) -> Optional[str]:
    """
    If response looks like MediaWiki JSON error payload, return error.code (lowercased), else None.
    Safe: never raises.
    """
    try:
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "json" not in ctype:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return None
        err = data.get("error")
        if not isinstance(err, dict):
            return None
        code = err.get("code")
        if isinstance(code, str) and code:
            return code.lower()
    except Exception:
        return None
    return None


def fetch_url(
    url,
    params=None,
    send_json=None,
    data=None,
    return_json=False,
    content=False,
    method="GET",
    session=None,
    timeout=REQUEST_TIMEOUT,
    headers=None,
    *,
    max_retries=MAX_RETRIES,
    base_backoff=BASE_BACKOFF,
    max_backoff=MAX_BACKOFF,
    retry_read_timeout=True,
):
    """
    Fetch a URL using an adaptive, per-host throttled HTTP client.

    This function replaces the legacy fetch implementation and is safe to use
    from multiple threads. It integrates tightly with a module-global
    AdaptiveThrottle instance to enforce per-host rate limits, concurrency
    limits, and dynamic backoff in response to server feedback.

    The fetch loop itself only implements *short, bounded retries* for truly
    transient failures; long-term pacing and cooldown behavior is delegated
    entirely to the throttle.

    Parameters
    ----------
    url : str
        Fully qualified URL to fetch. May be an API endpoint or resource URL.
        The host portion is used to select a throttle profile.

    params : dict, optional
        Query parameters to include in the request. For GET requests, these
        are appended to the URL query string. For non-GET requests, they are
        still sent as query parameters if provided.

    send_json : dict, optional
        JSON payload to send in the request body for non-GET methods
        (POST, PATCH, PUT, DELETE). Ignored for GET requests.

    return_json : bool, default False
        If True, parse and return the response body as JSON (via response.json()).
        Raises if the response body cannot be parsed as JSON.

    content : bool, default False
        If True, return the raw response content (bytes).
        If both return_json and content are False, response.text is returned.

    method : str, default "GET"
        HTTP method to use. Must correspond to a valid requests method
        (e.g. "GET", "POST", "PATCH", "DELETE").

    session : requests.Session, optional
        Optional requests.Session to use for connection pooling and reuse.
        If not provided, the top-level requests module is used.

    timeout : float, default REQUEST_TIMEOUT
        Per-request timeout in seconds, passed directly to requests.

    headers : dict, optional
        HTTP headers to include in the request. If not provided, the module’s
        default User-Agent header is used.

    max_retries : int, default MAX_RETRIES
        Maximum number of attempts for this request. Applies to:
          - network timeouts
          - connection errors
          - transient HTTP errors (408, 5xx)
          - HTTP 429 (rate-limited), which is retried after throttle gating

    base_backoff : float, default BASE_BACKOFF
        Base backoff duration (seconds) for exponential backoff applied to
        transient errors (not 429).

    max_backoff : float, default MAX_BACKOFF
        Maximum backoff duration (seconds) for transient error retries.

    Returns
    -------
    str | bytes | dict
        The response body, returned as:
          - dict if return_json=True
          - bytes if content=True
          - str otherwise

    Raises
    ------
    FetchUrlFailError
        If the request fails after exhausting all retries.

    RuntimeError
        For non-retryable HTTP errors such as 404, with response text preserved.

    requests.RequestException
        For unexpected or non-transient requests errors.

    Behavior Notes
    --------------
    - All attempts are gated by AdaptiveThrottle.acquire(), enforcing per-host
      rate and concurrency limits.
    - HTTP 429 responses are retried up to max_retries, but no exponential
      backoff is applied in this function; the throttle enforces Retry-After
      and cooldown behavior.
    - MediaWiki JSON error payloads are auto-detected:
        * error.code == "ratelimited" → treated like HTTP 429
        * error.code == "maxlag"     → treated as transient overload
    - The function is thread-safe and suitable for high-volume crawling or
      archival workloads.
    """

    client = session or requests
    hdrs = headers or _url_headers

    req = getattr(client, method.lower(), None)
    if req is None:
        raise ValueError(f"fetch_url: unsupported method {method}")

    attempt = 0
    last_exception = None

    while attempt < max_retries:
        kwargs = {"timeout": timeout, "headers": hdrs}
        if method.upper() == "GET":
            kwargs["params"] = params
        else:
            if params is not None:
                kwargs["params"] = params
            if send_json is not None:
                kwargs["json"] = send_json
            if data is not None:
                kwargs["data"] = data
        try:
            with THROTTLE.acquire(url):
                response = req(url, **kwargs)

            # MediaWiki JSON “error.code” detection
            mw_code = _maybe_mediawiki_error_code(response)

            # Feed throttle: treat MW ratelimit payload like 429
            THROTTLE.report_http(
                url,
                response.status_code,
                response.headers,
                is_rate_limited_payload=(mw_code in _MW_RATE_LIMIT_CODES),
            )

            # report throttle state periodically
            _maybe_log_throttle_report()

            # MW “maxlag” is a transient overload signal (treat like transient)
            if mw_code in _MW_TRANSIENT_CODES:
                raise _TransientPayload(mw_code)
            
            # MW “ratelimited” (even if HTTP 200) should behave like 429 retry
            if mw_code in _MW_RATE_LIMIT_CODES:
                raise _RateLimitedPayload(mw_code)

            # 429: retryable, but DO NOT exponential-sleep here; throttle will gate the next attempt
            if response.status_code == 429:
                raise requests.exceptions.HTTPError(
                    f"HTTP 429: {getattr(response, 'text', '')}",
                    response=response,
                )

            if response.status_code in _TRANSIENT_HTTP:
                raise requests.exceptions.HTTPError(
                    f"Transient HTTP {response.status_code}",
                    response=response,
                )

            if not response.ok:
                if response.status_code == 404:
                    raise RuntimeError(f"Failed to fetch (404): {url} :: {response.text}")
                raise requests.exceptions.HTTPError(
                    f"HTTP {response.status_code}: {response.text}",
                    response=response,
                )

            if return_json:
                return response.json()
            if content:
                return response.content
            return response.text

        except requests.exceptions.ReadTimeout as e:
            # ReadTimeout means the request reached the server but no response came back.
            # For non-idempotent operations (e.g. POST), retrying may create duplicates.
            if not retry_read_timeout:
                raise FetchUrlFailError(str(e)) from e
            last_exception = e
            THROTTLE.report_exception(url)

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            THROTTLE.report_exception(url)

        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)

            # Retry 429 (and MW ratelimited which may present as 200): NO extra sleep besides throttle gating.
            if status == 429:
                last_exception = e
                # tiny jitter to avoid tight spin if Retry-After is missing/misparsed
                time.sleep(random.uniform(0.05, 0.15))
                attempt += 1
                continue

            # Retry only known transient statuses (including MW maxlag mapped above)
            if status is not None and status not in _TRANSIENT_HTTP:
                raise

            last_exception = e

        except requests.RequestException as e:
            THROTTLE.report_exception(url)
            raise FetchUrlFailError(str(e)) from e

        except _RateLimitedPayload as e:
            last_exception = e
            # throttle already updated; next acquire will block
            time.sleep(random.uniform(0.05, 0.15))
            attempt += 1
            continue

        except _TransientPayload as e:
            last_exception = e
            THROTTLE.report_exception(url)  # or a dedicated transient report
            # then fall through to exponential backoff path by not continuing

        # For transient errors (not 429), do short exponential backoff
        wait = min(max_backoff, base_backoff * (2 ** attempt))
        wait += random.uniform(0, 0.5)
        _logger.info(
            f"[{attempt+1}/{max_retries}] Error: {last_exception}. Retrying in {wait:.2f}s..."
        )
        time.sleep(wait)
        attempt += 1

    raise FetchUrlFailError(
        f"Failed to fetch after {max_retries} retries: {url}"
    ) from last_exception

