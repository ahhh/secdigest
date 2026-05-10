"""_fetch_article_text follows redirects with per-hop SSRF re-validation.

Past incident: a user imported a Blogger post and got the summary
"Unable to retrieve article content" because Blogger issues 30x redirects
(canonical URL, consent/region flows) and the prior implementation had
`follow_redirects=False` with no manual re-walk. The fix walks the
redirect chain ourselves, re-running `is_safe_external_url` on every hop
so a 302 → private IP still can't bypass the guard.

These tests are self-contained: `httpx.Client` and `is_safe_external_url`
are both monkeypatched, so nothing here touches DNS or the network.
"""
import ipaddress
import types
from urllib.parse import urlparse

import httpx
import pytest

from secdigest import summarizer


# ── Test doubles ─────────────────────────────────────────────────────────────

def _resp(status: int, *, location: str | None = None,
          text: str = "", content_type: str = "text/html"):
    """Stand-in for an httpx.Response with just enough surface for the fetcher."""
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
    """Replace httpx.Client with a scripted-response fake. Each call to
    .get(url) records the URL and pops the next pre-loaded response."""
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
    """Allow-list for is_safe_external_url so tests don't hit DNS. Mutate
    the returned set to mark hostnames as safe; private-IP literals are
    always rejected (mirrors the real function's defence)."""
    safe_hosts: set[str] = set()

    def _check(url: str) -> bool:
        host = urlparse(url).hostname
        if not host:
            return False
        # Private/loopback/link-local IPs are NEVER safe — preserves the
        # real SSRF defence for redirect tests that target internal IPs.
        try:
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False
        except ValueError:
            pass
        return host in safe_hosts

    monkeypatch.setattr(summarizer, "is_safe_external_url", _check)
    return safe_hosts


# ── Happy path: redirects are followed end-to-end ────────────────────────────

def test_fetch_article_text_follows_302_to_safe_target(fake_httpx, patch_safe):
    """Blogger and similar publishers issue 30x for canonical/consent flows.
    The fetcher must follow them, not silently bail at the first 302."""
    patch_safe.add("blog.example.com")
    patch_safe.add("blog.example.com.au")

    fake_httpx.script(
        _resp(302, location="https://blog.example.com.au/post.html"),
        _resp(200, text="<article>Real content here.</article>"),
    )

    result = summarizer._fetch_article_text("https://blog.example.com/post.html")

    assert "Real content here" in result
    assert fake_httpx.requests == [
        "https://blog.example.com/post.html",
        "https://blog.example.com.au/post.html",
    ]


def test_fetch_article_text_walks_a_chain_of_redirects(fake_httpx, patch_safe):
    """301 → 302 → 200 should all chain through transparently."""
    patch_safe.update({"a.example.com", "b.example.com", "c.example.com"})
    fake_httpx.script(
        _resp(301, location="https://b.example.com/"),
        _resp(302, location="https://c.example.com/"),
        _resp(200, text="<main>final landing</main>"),
    )
    result = summarizer._fetch_article_text("https://a.example.com/")
    assert "final landing" in result
    assert fake_httpx.requests[-1] == "https://c.example.com/"


def test_fetch_article_text_relative_redirect_resolves_against_current_hop(
        fake_httpx, patch_safe):
    """A bare path in Location must resolve relative to the URL we just
    fetched — not the original. Common shape: `Location: /canonical/x`."""
    patch_safe.add("blog.example.com")
    fake_httpx.script(
        _resp(302, location="/canonical/post.html"),
        _resp(200, text="<article>landed</article>"),
    )
    result = summarizer._fetch_article_text("https://blog.example.com/post.html")
    assert "landed" in result
    assert fake_httpx.requests[1] == "https://blog.example.com/canonical/post.html"


# ── SSRF defence on the redirect target ──────────────────────────────────────

def test_fetch_article_text_blocks_redirect_to_private_ip(
        fake_httpx, patch_safe):
    """The headline SSRF case: a publicly-resolvable URL must not be able
    to redirect us to an internal address, even with redirect-following on."""
    patch_safe.add("evil.example.com")
    fake_httpx.script(
        _resp(302, location="http://192.168.1.1/admin"),
    )
    result = summarizer._fetch_article_text("https://evil.example.com/")
    assert result == ""
    # Critical: the second request must NEVER fire.
    assert fake_httpx.requests == ["https://evil.example.com/"]


def test_fetch_article_text_blocks_redirect_to_loopback(
        fake_httpx, patch_safe):
    patch_safe.add("evil.example.com")
    fake_httpx.script(_resp(302, location="http://127.0.0.1/"))
    assert summarizer._fetch_article_text("https://evil.example.com/") == ""
    assert len(fake_httpx.requests) == 1


# ── Bounds + bail-out paths ──────────────────────────────────────────────────

def test_fetch_article_text_caps_redirect_chain(fake_httpx, patch_safe):
    """Endless 302 loops — abandon after the per-call cap so a misconfigured
    publisher can't peg the worker."""
    patch_safe.add("a.example.com")
    # _MAX_REDIRECTS + 1 sequential 302s, all to the same allow-listed host.
    fake_httpx.script(*[
        _resp(302, location="https://a.example.com/")
        for _ in range(summarizer._MAX_REDIRECTS + 1)
    ])
    result = summarizer._fetch_article_text("https://a.example.com/")
    assert result == ""


def test_fetch_article_text_302_with_no_location_returns_empty(
        fake_httpx, patch_safe):
    """A redirect status with no Location header is malformed; bail rather
    than guess."""
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(302))  # no location
    assert summarizer._fetch_article_text("https://a.example.com/") == ""


def test_fetch_article_text_returns_empty_on_4xx(fake_httpx, patch_safe):
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(404, text="<html>not found</html>"))
    assert summarizer._fetch_article_text("https://a.example.com/") == ""


def test_fetch_article_text_skips_non_html_content_type(
        fake_httpx, patch_safe):
    """The parser below the fetch is text-oriented; PDFs/images/etc. are
    silently skipped rather than fed through a regex stripper."""
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(200, content_type="application/pdf",
                            text="binary garbage"))
    assert summarizer._fetch_article_text("https://a.example.com/") == ""


def test_fetch_article_text_accepts_text_html_with_charset(
        fake_httpx, patch_safe):
    """`text/html; charset=utf-8` is the canonical form most servers send.
    Substring match should accept it."""
    patch_safe.add("a.example.com")
    fake_httpx.script(_resp(200, content_type="text/html; charset=utf-8",
                            text="<article>fine</article>"))
    assert "fine" in summarizer._fetch_article_text("https://a.example.com/")


# ── Initial-URL gates fire before any HTTP ───────────────────────────────────

def test_fetch_article_text_unsafe_initial_url_makes_no_request(
        fake_httpx, patch_safe):
    """If the FIRST URL fails the SSRF gate, no httpx call should happen.
    `patch_safe` is empty, so every URL is treated as unsafe."""
    result = summarizer._fetch_article_text("https://internal.example.com/")
    assert result == ""
    assert fake_httpx.requests == []


def test_fetch_article_text_skips_hn_comment_pages(fake_httpx, patch_safe):
    """HN comment-page URLs are filtered before the fetch even starts —
    they have no real article content."""
    patch_safe.add("news.ycombinator.com")
    result = summarizer._fetch_article_text(
        "https://news.ycombinator.com/item?id=12345")
    assert result == ""
    assert fake_httpx.requests == []


def test_fetch_article_text_returns_empty_on_empty_url(
        fake_httpx, patch_safe):
    assert summarizer._fetch_article_text("") == ""
    assert fake_httpx.requests == []
