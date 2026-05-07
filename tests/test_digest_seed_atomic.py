"""Tests for digest_seed atomicity (L2 / phase 3).

The function does DELETE-then-loop-INSERT; we wrap it in an implicit sqlite3
transaction (`with conn:`) so a crash mid-loop rolls back to the previous
state instead of leaving the digest half-populated.

Two scenarios:
  • Happy path — every selected article lands in digest_articles
  • Failure path — a mocked exception during the INSERT loop must roll back
    every prior INSERT *and* the leading DELETE
"""
import pytest

from secdigest import db


def _seed_articles_in_week():
    """Seed three daily newsletters with one article each within ISO week 18 / 2026."""
    days = ("2026-04-29", "2026-04-30", "2026-05-01")
    aids = []
    for d, score in zip(days, (8.0, 7.0, 6.0)):
        n = db.newsletter_get_or_create(d)
        aids.append(db.article_insert(
            newsletter_id=n["id"], hn_id=None, title=f"art-{d}",
            url=f"https://x/{d}", hn_score=0, hn_comments=0,
            relevance_score=score, relevance_reason="r", position=0, included=1,
        ))
    return aids


def test_digest_seed_writes_every_selected_article(tmp_db):
    aids = _seed_articles_in_week()
    digest = db.newsletter_get_or_create(
        "2026-04-27", kind="weekly",
        period_start="2026-04-27", period_end="2026-05-03",
    )
    db.digest_seed(digest["id"], kind="weekly",
                   period_start="2026-04-27", period_end="2026-05-03", top_n=10)

    rows = db.digest_article_list(digest["id"])
    assert {r["id"] for r in rows} == set(aids)


class _FlakyConn:
    """Proxy around a real sqlite3.Connection that raises on the Nth matching
    INSERT. We can't monkeypatch sqlite3.Connection.execute directly — the
    method lives in a C extension and the slot is read-only. Wrapping the
    whole connection in a proxy lets us inject failures while leaving every
    other method untouched."""

    def __init__(self, real, fail_on_nth_insert: int):
        self._real = real
        self._target = fail_on_nth_insert
        self._insert_count = 0

    def execute(self, sql, params=()):
        if "INSERT INTO digest_articles" in sql:
            self._insert_count += 1
            if self._insert_count == self._target:
                raise RuntimeError("simulated mid-loop failure")
        return self._real.execute(sql, params)

    # `with conn:` and `conn.commit()/rollback()` must hit the real connection
    # so the transaction state is consistent.
    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    # Anything else (cursor, close, executemany, ...) delegates transparently.
    def __getattr__(self, name):
        return getattr(self._real, name)


def test_digest_seed_rolls_back_on_mid_loop_failure(tmp_db, monkeypatch):
    """Pre-seed the digest with one article, then have digest_seed fail partway
    through its INSERT loop. The transaction wrapper must roll back the
    half-applied DELETE+INSERTs so the original article is still present."""
    aids = _seed_articles_in_week()
    digest = db.newsletter_get_or_create(
        "2026-04-27", kind="weekly",
        period_start="2026-04-27", period_end="2026-05-03",
    )
    # Pre-seed with exactly one row so we can detect rollback unambiguously
    db.digest_article_add(digest["id"], aids[0], position=0, included=1)
    before = db.digest_article_list(digest["id"])
    assert len(before) == 1

    # Swap the module-level connection out for the flaky proxy. db._get_conn()
    # returns the cached _conn singleton, so replacing it routes every
    # subsequent execute through our wrapper.
    flaky = _FlakyConn(db._conn, fail_on_nth_insert=2)
    monkeypatch.setattr(db, "_conn", flaky)

    with pytest.raises(RuntimeError, match="simulated"):
        db.digest_seed(digest["id"], kind="weekly",
                       period_start="2026-04-27", period_end="2026-05-03", top_n=10)

    # Roll-back invariant: the digest should be back to its pre-seed state
    # (one row, the pre-existing pin), NOT empty and NOT half-populated.
    after = db.digest_article_list(digest["id"])
    assert {r["id"] for r in after} == {aids[0]}, \
        f"transaction did not roll back; digest now has: {[r['id'] for r in after]}"
