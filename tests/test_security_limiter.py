"""Tests for the per-IP rate-limit buckets in secdigest.web.security.

Focus: M2 (bounded dicts) and the cross-bucket isolation. The integration
tests in test_public_site.py cover the user-visible 429 behaviour; this file
asserts the storage-level invariants directly.
"""
import time as _time
from unittest.mock import Mock

import pytest

from secdigest.web import security


def _req(ip: str = "1.2.3.4"):
    """Build a minimal stand-in request with .client.host and headers — enough
    to feed into _client_ip without spinning up the full Starlette stack."""
    r = Mock()
    r.client = Mock(host=ip)
    r.headers = {}
    return r


@pytest.fixture(autouse=True)
def _clear_buckets():
    security._LOGIN_ATTEMPTS.clear()
    security._SUBSCRIBE_ATTEMPTS.clear()
    security._UNSUBSCRIBE_ATTEMPTS.clear()
    yield
    security._LOGIN_ATTEMPTS.clear()
    security._SUBSCRIBE_ATTEMPTS.clear()
    security._UNSUBSCRIBE_ATTEMPTS.clear()


# ── Empty-bucket eviction ───────────────────────────────────────────────────

def test_subscribe_bucket_evicts_empty_keys_after_window(monkeypatch):
    """After a record, the IP's bucket has one entry. After the window passes,
    the next allowed-check must both report the IP as eligible AND remove its
    key from the dict (the unbounded-growth fix)."""
    base = 1_000_000.0
    monkeypatch.setattr(security, "time", lambda: base)
    security.subscribe_record(_req("9.9.9.9"))
    assert "9.9.9.9" in security._SUBSCRIBE_ATTEMPTS

    # Jump past the window and re-check
    monkeypatch.setattr(security, "time", lambda: base + security._SUBSCRIBE_WINDOW_SECONDS + 1)
    assert security.subscribe_allowed(_req("9.9.9.9"))
    assert "9.9.9.9" not in security._SUBSCRIBE_ATTEMPTS, \
        "stale empty key was not evicted"


def test_login_bucket_evicts_after_window(monkeypatch):
    base = 2_000_000.0
    monkeypatch.setattr(security, "time", lambda: base)
    security.login_record_failure(_req("8.8.8.8"))
    assert "8.8.8.8" in security._LOGIN_ATTEMPTS

    monkeypatch.setattr(security, "time", lambda: base + security._LOGIN_WINDOW_SECONDS + 1)
    assert security.login_allowed(_req("8.8.8.8"))
    assert "8.8.8.8" not in security._LOGIN_ATTEMPTS


# ── Forced sweep at the safety cap ──────────────────────────────────────────

def test_subscribe_bucket_sweeps_when_above_safety_cap(monkeypatch):
    """When the bucket grows past _BUCKET_MAX_KEYS, the next record() must
    trigger a sweep that purges every IP whose timestamps are all older than
    the window. Simulates an attacker spraying unique IPs."""
    monkeypatch.setattr(security, "_BUCKET_MAX_KEYS", 5)
    base = 3_000_000.0

    # 5 stale entries (well outside the window)
    monkeypatch.setattr(security, "time",
                         lambda: base - security._SUBSCRIBE_WINDOW_SECONDS - 100)
    for i in range(5):
        security.subscribe_record(_req(f"stale-{i}"))
    assert len(security._SUBSCRIBE_ATTEMPTS) == 5

    # Now jump back to "now" and add one more — that pushes us over the cap
    # of 5 and triggers the sweep, which should drop all 5 stale entries.
    monkeypatch.setattr(security, "time", lambda: base)
    security.subscribe_record(_req("fresh"))

    keys = set(security._SUBSCRIBE_ATTEMPTS.keys())
    assert keys == {"fresh"}, f"sweep didn't purge stale keys: {keys}"


# ── Cross-bucket isolation ──────────────────────────────────────────────────

def test_subscribe_record_does_not_affect_login_bucket():
    security.subscribe_record(_req("1.1.1.1"))
    security.subscribe_record(_req("1.1.1.1"))
    # Login bucket for the same IP should still be untouched
    assert security._LOGIN_ATTEMPTS == {}
    assert security.login_allowed(_req("1.1.1.1"))


def test_unsubscribe_record_does_not_affect_subscribe_bucket():
    for _ in range(security._UNSUBSCRIBE_MAX):
        security.unsubscribe_record(_req("2.2.2.2"))
    assert not security.unsubscribe_allowed(_req("2.2.2.2"))
    # subscribe bucket on the same IP is independent
    assert security.subscribe_allowed(_req("2.2.2.2"))


# ── Limit enforcement ───────────────────────────────────────────────────────

def test_subscribe_allows_up_to_max_then_blocks(monkeypatch):
    base = 4_000_000.0
    monkeypatch.setattr(security, "time", lambda: base)
    for i in range(security._SUBSCRIBE_MAX):
        assert security.subscribe_allowed(_req("3.3.3.3")), \
            f"attempt {i+1} should still be allowed"
        security.subscribe_record(_req("3.3.3.3"))
    assert not security.subscribe_allowed(_req("3.3.3.3")), \
        "attempt past the limit should be denied"


def test_login_clear_drops_only_target_ip():
    security.login_record_failure(_req("4.4.4.4"))
    security.login_record_failure(_req("5.5.5.5"))
    security.login_clear(_req("4.4.4.4"))
    assert "4.4.4.4" not in security._LOGIN_ATTEMPTS
    assert "5.5.5.5" in security._LOGIN_ATTEMPTS
