import pytest
import secdigest.db as db_module
from secdigest import config


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Redirect all DB operations to a throwaway SQLite file for the duration of the test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    db_module._conn = None
    db_module.init_db()
    yield db_path
    if db_module._conn:
        db_module._conn.close()
        db_module._conn = None


@pytest.fixture
def mock_scheduler(monkeypatch):
    """Replace scheduler start/stop with no-ops so tests don't spin up APScheduler."""
    monkeypatch.setattr("secdigest.scheduler.start_scheduler", lambda: None)
    monkeypatch.setattr("secdigest.scheduler.stop_scheduler", lambda: None)
