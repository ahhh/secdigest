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


# ── Login rate limiting ─────────────────────────────────────────────────────

_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_SECONDS = 900  # 15 minutes
_LOGIN_MAX_ATTEMPTS = 10


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_allowed(request: Request) -> bool:
    """Check if the requesting IP is below the failed-login threshold."""
    ip = _client_ip(request)
    now = time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    attempts = _LOGIN_ATTEMPTS[ip]
    attempts[:] = [t for t in attempts if t > cutoff]
    return len(attempts) < _LOGIN_MAX_ATTEMPTS


def login_record_failure(request: Request) -> None:
    ip = _client_ip(request)
    _LOGIN_ATTEMPTS[ip].append(time())


def login_clear(request: Request) -> None:
    ip = _client_ip(request)
    _LOGIN_ATTEMPTS.pop(ip, None)
