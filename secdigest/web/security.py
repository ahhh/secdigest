"""Security helpers: SSRF guards, login rate limiting."""
import ipaddress
import socket
from collections import defaultdict
from time import time
from urllib.parse import urlparse

from fastapi import Request


def is_safe_external_url(url: str) -> bool:
    """Return True only if url is http(s) and resolves to a non-private public IP."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        addrs = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
            addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
        except Exception:
            return False
    for ip in addrs:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


# ── Per-IP rate limiting (login + subscribe + unsubscribe) ─────────────────
#
# Sliding-window buckets keyed by client IP. The buckets share the same
# implementation; they're separate dicts so a brute-force login spree can't
# starve the subscribe flow on a shared NAT'd IP, and so each can carry its
# own threshold/window.
#
# Memory bound: each bucket prunes both stale timestamps AND empty keys on
# every access, so the dict only ever holds IPs with at least one attempt in
# the current window. As a defensive cap (in case a flood of unique IPs
# arrives faster than the cleanup pace), if a bucket grows past
# _BUCKET_MAX_KEYS we run a full sweep before accepting more entries.

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_SUBSCRIBE_ATTEMPTS: dict[str, list[float]] = {}
_UNSUBSCRIBE_ATTEMPTS: dict[str, list[float]] = {}
_FEEDBACK_ATTEMPTS: dict[str, list[float]] = {}

_LOGIN_WINDOW_SECONDS = 900           # 15 minutes
_LOGIN_MAX_ATTEMPTS = 10
_SUBSCRIBE_WINDOW_SECONDS = 3600      # 1 hour
_SUBSCRIBE_MAX = 5
_UNSUBSCRIBE_WINDOW_SECONDS = 3600
_UNSUBSCRIBE_MAX = 10
# Feedback is mailed daily/weekly/monthly so a hot inbox can produce ~5–10 clicks
# in quick succession (multiple devices, accidental double-clicks). Set the cap
# loosely — the UNIQUE(subscriber, newsletter) constraint means duplicate clicks
# are upserts, not new rows, so abuse cost is bounded server-side anyway.
_FEEDBACK_WINDOW_SECONDS = 3600
_FEEDBACK_MAX = 60

_BUCKET_MAX_KEYS = 10_000             # absolute cap before a forced sweep


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _bucket_allowed(bucket: dict, ip: str, window: int, limit: int) -> bool:
    """Prune stale timestamps for `ip`, drop the key if its list is empty,
    return whether the IP is still under `limit` attempts in `window` seconds."""
    cutoff = time() - window
    attempts = [t for t in bucket.get(ip, ()) if t > cutoff]
    if attempts:
        bucket[ip] = attempts
    else:
        bucket.pop(ip, None)
    return len(attempts) < limit


def _bucket_record(bucket: dict, ip: str, window: int) -> None:
    """Append a timestamp; trigger a full-sweep cleanup if the dict has grown
    past the safety cap (an indicator that something pathological is going on,
    e.g. an attacker spraying unique IPs faster than the natural eviction)."""
    bucket.setdefault(ip, []).append(time())
    if len(bucket) > _BUCKET_MAX_KEYS:
        _bucket_sweep(bucket, window)


def _bucket_sweep(bucket: dict, window: int) -> None:
    """Drop every IP whose attempts are entirely outside the current window."""
    cutoff = time() - window
    stale = [ip for ip, ts in bucket.items() if not any(t > cutoff for t in ts)]
    for ip in stale:
        del bucket[ip]


# ── Public bucket APIs ──────────────────────────────────────────────────────

def login_allowed(request: Request) -> bool:
    return _bucket_allowed(_LOGIN_ATTEMPTS, _client_ip(request),
                            _LOGIN_WINDOW_SECONDS, _LOGIN_MAX_ATTEMPTS)


def login_record_failure(request: Request) -> None:
    _bucket_record(_LOGIN_ATTEMPTS, _client_ip(request), _LOGIN_WINDOW_SECONDS)


def login_clear(request: Request) -> None:
    _LOGIN_ATTEMPTS.pop(_client_ip(request), None)


def subscribe_allowed(request: Request) -> bool:
    return _bucket_allowed(_SUBSCRIBE_ATTEMPTS, _client_ip(request),
                            _SUBSCRIBE_WINDOW_SECONDS, _SUBSCRIBE_MAX)


def subscribe_record(request: Request) -> None:
    _bucket_record(_SUBSCRIBE_ATTEMPTS, _client_ip(request), _SUBSCRIBE_WINDOW_SECONDS)


def unsubscribe_allowed(request: Request) -> bool:
    return _bucket_allowed(_UNSUBSCRIBE_ATTEMPTS, _client_ip(request),
                            _UNSUBSCRIBE_WINDOW_SECONDS, _UNSUBSCRIBE_MAX)


def unsubscribe_record(request: Request) -> None:
    _bucket_record(_UNSUBSCRIBE_ATTEMPTS, _client_ip(request), _UNSUBSCRIBE_WINDOW_SECONDS)


def feedback_allowed(request: Request) -> bool:
    return _bucket_allowed(_FEEDBACK_ATTEMPTS, _client_ip(request),
                            _FEEDBACK_WINDOW_SECONDS, _FEEDBACK_MAX)


def feedback_record_attempt(request: Request) -> None:
    _bucket_record(_FEEDBACK_ATTEMPTS, _client_ip(request), _FEEDBACK_WINDOW_SECONDS)
