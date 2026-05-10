"""Fetch and parse RSS/Atom feeds.

The fetcher pulls articles from two kinds of source: Hacker News (handled
elsewhere) and arbitrary RSS/Atom feeds operators add via the admin UI.
This module owns the second case. The output shape is intentionally
identical to the HN article shape so the scoring/ranking step downstream
can treat both sources uniformly.

We use:
- ``httpx`` for the HTTP fetch (sync client, short timeout, no redirects)
- ``defusedxml`` for the parse — the stdlib XML parser is vulnerable to
  XXE / billion-laughs attacks against attacker-controlled content. Since
  feed URLs are user-configured, we treat the response as untrusted XML.
"""
import re
from urllib.parse import urljoin

import httpx
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as _safe_fromstring

from secdigest.web.security import is_safe_external_url

# Atom feeds put their elements under this XML namespace. ElementTree
# represents namespaced tags as ``{namespace}localname``.
_ATOM_NS = 'http://www.w3.org/2005/Atom'

# Cap on hops we'll walk through 30x responses before giving up. Five covers
# the common publisher patterns (canonical-URL redirect, scheme upgrade,
# host swap when a publication moves) without letting a misconfigured feed
# pin the worker.
_MAX_REDIRECTS = 5


def _parse_rss(root: ET.Element, max_articles: int) -> list[dict]:
    """Extract items from an RSS 2.0 ``<channel>`` element.

    RSS structure: <rss><channel><item><title/><link/></item>...
    We take only ``max_articles`` items off the top so a noisy feed
    can't blow up the daily pool with hundreds of stories.
    """
    channel = root.find('channel')
    if channel is None:
        return []
    results = []
    for item in channel.findall('item')[:max_articles]:
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        # Drop any item missing the basics — without both we can't render
        # or dedup it later.
        if title and link:
            results.append({'title': title, 'url': link})
    return results


def _parse_atom(root: ET.Element, max_articles: int) -> list[dict]:
    """Extract entries from an Atom 1.0 feed.

    Atom differs from RSS in two ways that matter here:
    - Tags live under the Atom namespace (handled via ``_ATOM_NS``).
    - The link is in an attribute (``<link href="..."/>``) rather than
      element text. If there's no link, some feeds put the canonical
      URL in ``<id>`` instead, so we fall back to that.
    """
    results = []
    for entry in root.findall(f'{{{_ATOM_NS}}}entry')[:max_articles]:
        title = (entry.findtext(f'{{{_ATOM_NS}}}title') or '').strip()
        link_el = entry.find(f'{{{_ATOM_NS}}}link')
        link = link_el.get('href', '') if link_el is not None else ''
        if not link:
            link = (entry.findtext(f'{{{_ATOM_NS}}}id') or '').strip()
        # ``startswith('http')`` filters out tag: URIs and other non-fetchable ids.
        if title and link and link.startswith('http'):
            results.append({'title': title, 'url': link})
    return results


def fetch_feed(url: str, max_articles: int = 5) -> list[dict]:
    """Fetch and parse a single RSS/Atom feed. Returns [{title, url}]."""
    # SSRF guard: feed URLs come from operator input, so we refuse to
    # fetch anything pointing at localhost / private IP space / file://.
    if not is_safe_external_url(url):
        return []
    try:
        # Follow redirects manually so the SSRF guard runs on EVERY hop —
        # bare follow_redirects=True would let a 302 → internal IP through.
        # Common cases: a publisher moves their feed to a new host, an http
        # → https upgrade, a /feed → /feed/ canonicalisation. Custom UA is
        # polite (some feeds reject the default httpx UA).
        with httpx.Client(follow_redirects=False, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; SecDigest/1.0)"}) as client:
            current = url
            resp = None
            for _ in range(_MAX_REDIRECTS + 1):
                resp = client.get(current)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get("location", "")
                if not loc:
                    print(f"[rss] {url}: {resp.status_code} with no Location header")
                    return []
                # Resolve relative redirects against the URL we just fetched,
                # not the original — matters for `Location: /feed.xml`.
                next_url = urljoin(current, loc)
                if not is_safe_external_url(next_url):
                    print(f"[rss] {url}: redirect to unsafe URL blocked ({next_url})")
                    return []
                current = next_url
            else:
                print(f"[rss] {url}: too many redirects (>{_MAX_REDIRECTS})")
                return []
        if resp.status_code != 200:
            print(f"[rss] {url}: HTTP {resp.status_code}")
            return []
        # Strip up to 5 xmlns declarations so namespace-heavy feeds parse
        # more predictably across RSS variants. The Atom branch below still
        # references the full namespace, so well-formed Atom feeds keep working.
        text = re.sub(r' xmlns(?::\w+)?="[^"]+"', '', resp.text, count=5)
        root = _safe_fromstring(text)
    except Exception as e:
        # Network errors, malformed XML, defusedxml security rejections —
        # any of these just yield "no articles from this feed today".
        print(f"[rss] error fetching {url}: {e}")
        return []

    # Pick a parser by the root element name. Atom uses ``<feed>``;
    # RSS uses ``<rss>``. The substring check tolerates either being
    # namespaced or bare.
    tag = root.tag.lower()
    if 'feed' in tag or 'atom' in tag:
        return _parse_atom(root, max_articles)
    return _parse_rss(root, max_articles)


def fetch_all_rss() -> list[dict]:
    """Fetch articles from all active RSS feeds. Returns article dicts ready for scoring."""
    # Local import dodges a circular: ``db`` doesn't depend on ``rss``, but
    # importing it at module load would create a startup-time dependency.
    from secdigest import db
    feeds = db.rss_feed_active()
    all_articles = []
    for feed in feeds:
        articles = fetch_feed(feed['url'], feed.get('max_articles', 5))
        # Use the operator-supplied display name when present; fall back
        # to the URL so the curator UI has *something* to label by.
        feed_label = feed.get('name') or feed['url']
        # Reshape each article to match the HN-derived schema the rest of
        # the pipeline expects: id is filled in once stored, score/comments
        # default to 0 (no per-article ranking signal from a generic feed).
        for a in articles:
            a.update({'id': None, 'score': 0, 'comments': 0,
                      'source': 'rss', 'source_name': feed_label})
        all_articles.extend(articles)
        print(f"[rss] {feed_label}: {len(articles)} articles")
    return all_articles
