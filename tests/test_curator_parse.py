"""Curator JSON parsing is robust to Claude's response variance.

Prod incident (2026-05-14): the curator banner showed
    "Article curation failed unexpectedly
     Claude returned an error while scoring articles.
     Expecting value: line 1 column 1 (char 0)"
That's `json.JSONDecodeError` on an empty string — `_score_article` did
`json.loads(resp.content[0].text.strip())` directly, so the moment Claude
returned an empty/refusal/prose response the whole batch surfaced a scary
banner even though the keyword-score fallback had already filled in the
holes.

Fix: `_parse_curator_json` accepts the variance:
  - direct JSON, the happy path
  - JSON wrapped in prose ("Sure! Here's the score: {...}")
  - one-element array wrapping the dict (a known prompt-drift)
…and raises a *named* ValueError with the article title + response snippet
when the response really is unsalvageable, so the operator banner names
what went wrong instead of a generic decode error.
"""
import pytest

from secdigest.fetcher import _parse_curator_json


_ARTICLE = {"title": "Velvet Chollima Infostealer Campaign",
            "url": "https://example.com/x"}


def test_parses_plain_json_object():
    """Happy path — the curator prompt asks for this exact shape."""
    out = _parse_curator_json('{"score": 7, "reason": "infostealer report"}',
                              _ARTICLE)
    assert out == {"score": 7, "reason": "infostealer report"}


def test_unwraps_single_element_array():
    """Prompt drift: model sometimes returns [{...}] instead of {...}."""
    out = _parse_curator_json('[{"score": 4, "reason": "tangential"}]',
                              _ARTICLE)
    assert out == {"score": 4, "reason": "tangential"}


def test_recovers_json_embedded_in_prose():
    """Model prepends a chatty preamble — extract the first {...} block
    instead of failing the whole batch."""
    out = _parse_curator_json(
        'Sure! Here is the score: {"score": 8, "reason": "CVE"} — let me know.',
        _ARTICLE,
    )
    assert out == {"score": 8, "reason": "CVE"}


def test_empty_string_raises_named_error():
    """The exact prod symptom: empty text → JSONDecodeError used to bubble
    up as 'Expecting value: line 1 column 1 (char 0)'. Now it's a
    ValueError that names the article so the banner is actionable."""
    with pytest.raises(ValueError, match="Velvet Chollima"):
        _parse_curator_json("", _ARTICLE)


def test_non_json_non_object_raises_named_error():
    """Pure prose, no {...} anywhere — still must fail loudly, but with
    the article title and a snippet of what Claude actually said."""
    with pytest.raises(ValueError, match="Velvet Chollima"):
        _parse_curator_json("I can't score this article.", _ARTICLE)


def test_empty_array_wrapper_raises_named_error():
    """`[]` parses cleanly but has no dict to unwrap — guard against it
    so callers don't see an IndexError downstream."""
    with pytest.raises(ValueError, match="unexpected JSON shape"):
        _parse_curator_json("[]", _ARTICLE)


def test_array_of_non_dict_raises_named_error():
    """[1, 2, 3] is valid JSON but not a {score, reason} shape."""
    with pytest.raises(ValueError, match="unexpected JSON shape"):
        _parse_curator_json("[1, 2, 3]", _ARTICLE)
