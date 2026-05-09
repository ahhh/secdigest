"""Fetch and parse RSS/Atom feeds."""
import re
import httpx
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as _safe_fromstring

from secdigest.web.security import is_safe_external_url

_ATOM_NS = 'http://www.w3.org/2005/Atom'


def _parse_rss(root: ET.Element, max_articles: int) -> list[dict]:
    channel = root.find('channel')
    if channel is None:
        return []
    results = []
    for item in channel.findall('item')[:max_articles]:
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        if title and link:
            results.append({'title': title, 'url': link})
    return results


def _parse_atom(root: ET.Element, max_articles: int) -> list[dict]:
    results = []
    for entry in root.findall(f'{{{_ATOM_NS}}}entry')[:max_articles]:
        title = (entry.findtext(f'{{{_ATOM_NS}}}title') or '').strip()
        link_el = entry.find(f'{{{_ATOM_NS}}}link')
        link = link_el.get('href', '') if link_el is not None else ''
        if not link:
            link = (entry.findtext(f'{{{_ATOM_NS}}}id') or '').strip()
        if title and link and link.startswith('http'):
            results.append({'title': title, 'url': link})
    return results


def fetch_feed(url: str, max_articles: int = 5) -> list[dict]:
    """Fetch and parse a single RSS/Atom feed. Returns [{title, url}]."""
    if not is_safe_external_url(url):
        return []
    try:
        with httpx.Client(follow_redirects=False, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; SecDigest/1.0)"}) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return []
        text = re.sub(r' xmlns(?::\w+)?="[^"]+"', '', resp.text, count=5)
        root = _safe_fromstring(text)
    except Exception as e:
        print(f"[rss] error fetching {url}: {e}")
        return []

    tag = root.tag.lower()
    if 'feed' in tag or 'atom' in tag:
        return _parse_atom(root, max_articles)
    return _parse_rss(root, max_articles)


def fetch_all_rss() -> list[dict]:
    """Fetch articles from all active RSS feeds. Returns article dicts ready for scoring."""
    from secdigest import db
    feeds = db.rss_feed_active()
    all_articles = []
    for feed in feeds:
        articles = fetch_feed(feed['url'], feed.get('max_articles', 5))
        feed_label = feed.get('name') or feed['url']
        for a in articles:
            a.update({'id': None, 'score': 0, 'comments': 0,
                      'source': 'rss', 'source_name': feed_label})
        all_articles.extend(articles)
        print(f"[rss] {feed_label}: {len(articles)} articles")
    return all_articles
