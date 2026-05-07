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

    # Inject a failure on the second INSERT by patching conn.execute. This
    # mimics e.g. a process kill mid-loop; without `with conn:`, we'd see the
    # leading DELETE applied + first INSERT partially through, leaving the
    # digest in a state with fewer rows than before.
    real_conn = db._get_conn()
    real_execute = real_conn.execute
    n_calls = {"insert": 0}

    def flaky_execute(sql, params=()):
        if "INSERT INTO digest_articles" in sql:
            n_calls["insert"] += 1
            if n_calls["insert"] == 2:
                raise RuntimeError("simulated mid-loop failure")
        return real_execute(sql, params)

    monkeypatch.setattr(real_conn, "execute", flaky_execute)

    with pytest.raises(RuntimeError, match="simulated"):
        db.digest_seed(digest["id"], kind="weekly",
                       period_start="2026-04-27", period_end="2026-05-03", top_n=10)

    # Roll-back invariant: the digest should be back to its pre-seed state
    # (one row, the pre-existing pin), NOT empty and NOT half-populated.
    after = db.digest_article_list(digest["id"])
    assert {r["id"] for r in after} == {aids[0]}, \
        f"transaction did not roll back; digest now has: {[r['id'] for r in after]}"
