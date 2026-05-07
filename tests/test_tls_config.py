"""Tests for the TLS config layer in secdigest/config.py.

Covers:
  • resolve_tls_paths() prioritises explicit TLS_CERTFILE/TLS_KEYFILE
  • resolve_tls_paths() falls back to /etc/letsencrypt/live/<domain>/ via TLS_DOMAIN
  • validate_tls_config() returns None when TLS_ENABLED=0
  • validate_tls_config() raises a clear error when TLS_ENABLED=1 with no cert config
  • validate_tls_config() raises when cert/key files are missing
  • validate_tls_config() returns the resolved pair when files exist
  • run.py's _ssl_kwargs() yields the kwargs uvicorn expects (ssl_certfile / ssl_keyfile)

The tests monkey-patch the module-level constants in secdigest.config because
those are read once at import time — env-var changes after import don't take
effect on the constants. The validator reads the patched values directly.
"""
import pytest

from secdigest import config


# ── resolve_tls_paths ───────────────────────────────────────────────────────

def test_resolve_uses_explicit_paths_when_set(monkeypatch):
    monkeypatch.setattr(config, "TLS_CERTFILE", "/custom/cert.pem")
    monkeypatch.setattr(config, "TLS_KEYFILE", "/custom/key.pem")
    monkeypatch.setattr(config, "TLS_DOMAIN", "should-be-ignored.example")
    assert config.resolve_tls_paths() == ("/custom/cert.pem", "/custom/key.pem")


def test_resolve_falls_back_to_letsencrypt_layout(monkeypatch):
    monkeypatch.setattr(config, "TLS_CERTFILE", "")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "secdigest.example.com")
    cert, key = config.resolve_tls_paths()
    assert cert == "/etc/letsencrypt/live/secdigest.example.com/fullchain.pem"
    assert key == "/etc/letsencrypt/live/secdigest.example.com/privkey.pem"


def test_resolve_returns_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "TLS_CERTFILE", "")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "")
    assert config.resolve_tls_paths() == ("", "")


def test_resolve_requires_both_explicit_paths(monkeypatch):
    """Setting only TLS_CERTFILE without TLS_KEYFILE should fall back to
    TLS_DOMAIN, not produce a half-configured pair."""
    monkeypatch.setattr(config, "TLS_CERTFILE", "/only/cert.pem")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "fall.back.example")
    cert, key = config.resolve_tls_paths()
    assert cert.endswith("/fullchain.pem")
    assert key.endswith("/privkey.pem")


# ── validate_tls_config ─────────────────────────────────────────────────────

def test_validate_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "TLS_ENABLED", False)
    assert config.validate_tls_config() is None


def test_validate_raises_when_enabled_with_no_paths(monkeypatch):
    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", "")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "")
    with pytest.raises(RuntimeError) as exc_info:
        config.validate_tls_config()
    msg = str(exc_info.value)
    # The error message must point the user at their three options
    assert "TLS_DOMAIN" in msg
    assert "TLS_CERTFILE" in msg
    assert "TLS_ENABLED=0" in msg


def test_validate_raises_when_certfile_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", str(tmp_path / "missing-cert.pem"))
    monkeypatch.setattr(config, "TLS_KEYFILE", str(tmp_path / "missing-key.pem"))
    monkeypatch.setattr(config, "TLS_DOMAIN", "")
    with pytest.raises(RuntimeError, match="TLS_CERTFILE not readable"):
        config.validate_tls_config()


def test_validate_raises_when_keyfile_missing(monkeypatch, tmp_path):
    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("dummy cert")
    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", str(cert_file))
    monkeypatch.setattr(config, "TLS_KEYFILE", str(tmp_path / "missing-key.pem"))
    monkeypatch.setattr(config, "TLS_DOMAIN", "")
    with pytest.raises(RuntimeError, match="TLS_KEYFILE not readable"):
        config.validate_tls_config()


def test_validate_returns_paths_when_files_exist(monkeypatch, tmp_path):
    cert_file = tmp_path / "fullchain.pem"
    key_file = tmp_path / "privkey.pem"
    cert_file.write_text("dummy cert content")
    key_file.write_text("dummy key content")

    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", str(cert_file))
    monkeypatch.setattr(config, "TLS_KEYFILE", str(key_file))
    monkeypatch.setattr(config, "TLS_DOMAIN", "")

    result = config.validate_tls_config()
    assert result == (str(cert_file), str(key_file))


def test_validate_with_letsencrypt_domain_and_real_files(monkeypatch, tmp_path):
    """End-to-end: TLS_DOMAIN drives the Let's Encrypt layout, and if those
    derived paths exist, validate returns them."""
    le_dir = tmp_path / "live" / "secdigest.test"
    le_dir.mkdir(parents=True)
    (le_dir / "fullchain.pem").write_text("cert")
    (le_dir / "privkey.pem").write_text("key")

    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", "")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "secdigest.test")
    monkeypatch.setattr(config, "TLS_LETSENCRYPT_DIR", str(tmp_path / "live"))

    result = config.validate_tls_config()
    assert result is not None
    cert, key = result
    assert cert.endswith("/secdigest.test/fullchain.pem")
    assert key.endswith("/secdigest.test/privkey.pem")


# ── run.py wiring ───────────────────────────────────────────────────────────

def test_run_ssl_kwargs_empty_when_disabled(monkeypatch):
    """When TLS is off, _ssl_kwargs must return an empty dict so uvicorn.run()
    is invoked without ssl_* kwargs (plain HTTP)."""
    monkeypatch.setattr(config, "TLS_ENABLED", False)
    import importlib
    import run as run_module
    importlib.reload(run_module)
    assert run_module._ssl_kwargs() == {}


def test_run_ssl_kwargs_populated_when_enabled(monkeypatch, tmp_path):
    """When TLS is on with valid cert files, _ssl_kwargs must return exactly
    the kwargs uvicorn.run() expects: ssl_certfile and ssl_keyfile."""
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("c")
    key_file.write_text("k")
    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", str(cert_file))
    monkeypatch.setattr(config, "TLS_KEYFILE", str(key_file))
    monkeypatch.setattr(config, "TLS_DOMAIN", "")

    import importlib
    import run as run_module
    importlib.reload(run_module)
    kwargs = run_module._ssl_kwargs()
    assert kwargs == {"ssl_certfile": str(cert_file), "ssl_keyfile": str(key_file)}


def test_run_ssl_kwargs_propagates_validation_error(monkeypatch):
    """Misconfigured TLS at the run.py boundary must propagate the
    validate_tls_config error so the operator sees the actionable message."""
    monkeypatch.setattr(config, "TLS_ENABLED", True)
    monkeypatch.setattr(config, "TLS_CERTFILE", "")
    monkeypatch.setattr(config, "TLS_KEYFILE", "")
    monkeypatch.setattr(config, "TLS_DOMAIN", "")
    import importlib
    import run as run_module
    importlib.reload(run_module)
    with pytest.raises(RuntimeError, match="TLS_ENABLED=1 but no certificate"):
        run_module._ssl_kwargs()


# ── Defaults sanity ─────────────────────────────────────────────────────────

def test_tls_default_state_in_env_example():
    """The shipped .env.example should match the documented defaults so users
    who copy it as-is get the announced behaviour (TLS on, no cert paths set —
    they need to fill in TLS_DOMAIN before starting)."""
    from pathlib import Path
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text()
    assert "TLS_ENABLED=1" in text
    assert "TLS_DOMAIN=" in text
    assert "TLS_CERTFILE=" in text
    assert "TLS_KEYFILE=" in text
