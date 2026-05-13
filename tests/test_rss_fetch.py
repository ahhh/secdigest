"""rss.fetch_feed follows redirects with per-hop SSRF re-validation.

Same hazard / same fix as the summarizer's article fetcher (see
test_summarizer_fetch.py). RSS feed URLs are operator-typed at /feeds, so
the failure mode is rarer than the user-imported article case — but
publisher-side redirects still happen (host moves, scheme upgrades,
trailing-slash canonicalisations) and the prior `follow_redirects=False`
silently swallowed all of them.

Tests are self-contained: `httpx.Client` and `is_safe_external_url` are
both monkeypatched, so nothing here touches DNS or the network.
"""
import ipaddress
import types
from urllib.parse import urlparse

import httpx
import pytest

from secdigest import rss


# ── Test doubles ─────────────────────────────────────────────────────────────

# Smallest RSS payload that exercises both the parse path and the result
# shape downstream code depends on (`title` + `url`).
_RSS_BODY = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    '<item><title>Hello</title><link>https://blog.example.com/post-1</link></item>'
    '</channel></rss>'
)


def _resp(status: int, *, location: str | None = None,
          text: str = "", content_type: str = "application/rss+xml"):
    """Stand-in for an httpx.Response. RSS doesn't gate on content-type the
    way the summarizer does, but we set a realistic header anyway."""
    headers = {"content-type": content_type}
    if location is not None:
        headers["location"] = location
    return types.SimpleNamespace(status_code=status, headers=headers, text=text)


class _FakeHttpxKnob:
    """Holds the scripted response queue + the ordered request log."""
    def __init__(self):
        self.requests: list[str] = []
        self._responses: list = []

    def script(self, *responses):
        self._responses = list(responses)


@pytest.fixture
def fake_httpx(monkeypatch):
    """Replace httpx.Client with a scripted-response fake. Each .get(url)
    records the URL and pops the next pre-loaded response."""
    knob = _FakeHttpxKnob()

    class _FakeClient:
        def __init__(self, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def get(self, url, **_kwargs):
            knob.requests.append(url)
            if not knob._responses:
                raise AssertionError(
                    f"unexpected GET {url} — no scripted responses left"
                )
            return knob._responses.pop(0)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return knob


@pytest.fixture
def patch_safe(monkeypatch):
    """Allow-list for is_safe_external_url. Mutate the returned set to
    mark hostnames safe; private-IP literals are always rejected so SSRF
    tests retain real defence even with the gate stubbed."""
    safe_hosts: set[str] = set()

    def _check(url: str) -> bool:
        host = urlparse(url).hostname
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False
        except ValueError:
            pass
        return host in safe_hosts

    monkeypatch.setattr(rss, "is_safe_external_url", _check)
    return safe_hosts


# ── Happy path: redirects are followed end-to-end ────────────────────────────

def test_fetch_feed_follows_302_to_safe_target(fake_httpx, patch_safe):
    """Operator types `https://oldhost.example.com/feed.xml`; the publisher
    has moved the feed and 302s to the new host. The fetcher must follow,
    parse, and return articles — not silently drop everything."""
    patch_safe.update({"oldhost.example.com", "newhost.example.com"})
    fake_httpx.script(
        _resp(302, location="https://newhost.example.com/feed.xml"),
        _resp(200, text=_RSS_BODY),
    )

    articles = rss.fetch_feed("https://oldhost.example.com/feed.xml")

    assert articles == [{"title": "Hello",
                         "url": "https://blog.example.com/post-1"}]
    assert fake_httpx.requests == [
        "https://oldhost.example.com/feed.xml",
        "https://newhost.example.com/feed.xml",
    ]


def test_fetch_feed_walks_a_chain_of_redirects(fake_httpx, patch_safe):
    """301 → 302 → 200 must chain transparently — common when an http→https
    upgrade is followed by a trailing-slash canonicalisation."""
    patch_safe.update({"a.example.com", "b.example.com", "c.example.com"})
    fake_httpx.script(
        _resp(301, location="https://b.example.com/feed"),
        _resp(302, location="https://c.example.com/feed/"),
        _resp(200, text=_RSS_BODY),
    )
    articles = rss.fetch_feed("http://a.example.com/feed")
    assert len(articles) == 1
    assert articles[0]["title"] == "Hello"


def test_fetch_feed_relative_redirect_resolves_against_current_hop(
        fake_httpx, patch_safe):
    """A bare path in Location must resolve relative to the URL we just
    fetched — common shape for trailing-slash canonical redirects."""
    patch_safe.add("blog.example.com")
    fake_httpx.script(
        _resp(301, location="/feed/"),
        _resp(200, text=_RSS_BODY),
    )
    articles = rss.fetch_feed("https://blog.example.com/feed")
    assert len(articles) == 1
    assert fake_httpx.requests[1] == "https://blog.example.com/feed/"


# ── SSRF defence on the redirect target ──────────────────────────────────────

def test_fetch_feed_blocks_redirect_to_private_ip(fake_httpx, patch_safe):
    """The headline SSRF case: an attacker who controls a feed URL should
    not be able to use a 302 to reach internal infrastructure even with
    redirect-following on. The second request must NEVER fire."""
    patch_safe.add("evil.example.com")
    fake_httpx.script(_resp(302, location="http://192.168.1.1/admin"))

    assert rss.fetch_feed("https://evil.example.com/feed") == []
    assert fake_httpx.requests == ["https://evil.example.com/feed"]


def test_fetch_feed_blocks_redirect_to_loopback(fake_httpx, patch_safe):
    patch_safe.add("evil.example.com")
    fake_httpx.script(_resp(302, location="http://127.0.0.1/internal"))
    assert rss.fetch_feed("https://evil.example.com/feed") == []
    assert len(fake_httpx.requests) == 1


# ── Bounds + bail-out paths ──────────────────────────────────────────────────

def test_fetch_feed_caps_redirect_chain(fake_httpx, patch_safe):
    """Endless 302 loops — abandon after the per-call cap so a misconfigured
    publisher can't peg the worker."""
    patch_safe.add("a.example.com")
    fake_httpx.script(*[
        _resp(302, location="https://a.example.com/feed")
        for _ in range(rss._MAX_REDIRECTS + 1)
    ])
    assert rss.fetch_feed("https://a.example.com/feed") == []


def test_fetch_feed_302_with_no_location_returns_empty(
        fake_httpx, patch_safe):
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(302))  # no Location header
    assert rss.fetch_feed("https://a.example.com/feed") == []


def test_fetch_feed_returns_empty_on_4xx(fake_httpx, patch_safe):
    """Non-redirect, non-200 should yield no articles — and a logged
    status code so the operator can see why nothing came through."""
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(404, text="<html>not found</html>"))
    assert rss.fetch_feed("https://a.example.com/feed") == []


def test_fetch_feed_returns_empty_on_5xx(fake_httpx, patch_safe):
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(503, text="upstream broken"))
    assert rss.fetch_feed("https://a.example.com/feed") == []


# ── Initial-URL gate fires before any HTTP ───────────────────────────────────

def test_fetch_feed_unsafe_initial_url_makes_no_request(
        fake_httpx, patch_safe):
    """If the operator-typed URL itself fails the SSRF gate, no httpx call
    should happen. `patch_safe` is empty, so every URL is treated unsafe."""
    assert rss.fetch_feed("https://internal.example.com/feed") == []
    assert fake_httpx.requests == []


# ── Status-code branches downstream of the redirect walk ─────────────────────

def test_fetch_feed_redirect_then_4xx_returns_empty(fake_httpx, patch_safe):
    """Walked the redirect chain successfully, but the final hop is a 404.
    The post-loop `status_code != 200` check should fire and bail cleanly."""
    patch_safe.update({"old.example.com", "new.example.com"})
    fake_httpx.script(
        _resp(301, location="https://new.example.com/feed.xml"),
        _resp(404, text="<html>not found</html>"),
    )
    assert rss.fetch_feed("https://old.example.com/feed.xml") == []
    # Both hops fired — the 404 is from the redirected URL, not the original.
    assert fake_httpx.requests == [
        "https://old.example.com/feed.xml",
        "https://new.example.com/feed.xml",
    ]


# ── Missing defusedxml degrades gracefully ───────────────────────────────────
#
# Production incident (2026-05-13): the server's site-packages drifted out of
# sync with requirements.txt, so `import defusedxml` failed at rss.py load.
# That took the entire fetcher down — HN included — because fetcher.py does
# `from secdigest import rss as rss_module` and the import error propagated up
# through asyncio.wait_for, crashing run_fetch.
#
# Fix: rss.py wraps the import in try/except and sets `_safe_fromstring=None`
# on failure; fetch_feed bails before any HTTP. RSS is skipped, HN keeps
# working. We do NOT fall back to the stdlib parser — that would defeat the
# XXE/billion-laughs defence that was the whole reason defusedxml is here.

def test_rss_module_imports_even_when_defusedxml_missing(monkeypatch):
    """Simulate the production state: `defusedxml` not installed. The module
    must still import cleanly — anything else propagates back up to fetcher.py
    and takes HN ingest down with it."""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "defusedxml" or name.startswith("defusedxml."):
            raise ModuleNotFoundError("No module named 'defusedxml'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    # Drop the cached module so the reimport actually re-runs the top-level code.
    monkeypatch.delitem(sys.modules, "secdigest.rss", raising=False)

    reloaded = importlib.import_module("secdigest.rss")
    assert reloaded._safe_fromstring is None
    # Restore the normal module for any subsequent tests in this session.
    monkeypatch.setattr(builtins, "__import__", real_import)
    importlib.reload(reloaded)


def test_fetch_feed_no_op_when_defusedxml_missing(fake_httpx, patch_safe,
                                                  monkeypatch):
    """With no safe parser available, fetch_feed must return [] BEFORE making
    any HTTP request. Fetching first and then parsing-with-fallback would
    either crash on the missing parser or silently regress XXE defence."""
    monkeypatch.setattr(rss, "_safe_fromstring", None)
    patch_safe.add("blog.example.com")

    assert rss.fetch_feed("https://blog.example.com/feed") == []
    # The HTTP client was never called — bail happens before the request.
    assert fake_httpx.requests == []


def test_fetch_all_rss_returns_empty_when_defusedxml_missing(monkeypatch):
    """fetch_all_rss should iterate configured feeds but produce zero
    articles when the parser is unavailable. The fetcher pipeline treats this
    as "0 RSS today" and continues with HN — which is the whole point of the
    fix."""
    monkeypatch.setattr(rss, "_safe_fromstring", None)
    # Stub db.rss_feed_active so we don't need a real DB; the contract is
    # just "iterable of {url, name, max_articles}".
    fake_db = types.SimpleNamespace(rss_feed_active=lambda: [
        {"url": "https://blog.example.com/feed",
         "name": "example", "max_articles": 5},
    ])
    monkeypatch.setitem(__import__("sys").modules, "secdigest.db", fake_db)

    assert rss.fetch_all_rss() == []
