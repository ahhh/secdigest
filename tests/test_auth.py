"""Unit tests for password hashing — catches bcrypt backend compatibility issues early."""
from secdigest.web.auth import hash_password, verify_password, ensure_default_password
from secdigest import db


def test_hash_password_produces_bcrypt_string():
    h = hash_password("secdigest")
    assert isinstance(h, str)
    assert h.startswith("$2b$")


def test_verify_password_correct():
    h = hash_password("mypassword")
    assert verify_password("mypassword", h) is True


def test_verify_password_wrong():
    h = hash_password("mypassword")
    assert verify_password("wrongpassword", h) is False


def test_ensure_default_password_writes_hash(tmp_db):
    ensure_default_password()
    ph = db.cfg_get("password_hash")
    assert ph and ph.startswith("$2b$")
    assert verify_password("secdigest", ph)


def test_ensure_default_password_is_idempotent(tmp_db):
    ensure_default_password()
    first = db.cfg_get("password_hash")
    ensure_default_password()
    assert db.cfg_get("password_hash") == first


def test_ensure_default_password_skips_if_already_set(tmp_db):
    existing = hash_password("custom")
    db.cfg_set("password_hash", existing)
    ensure_default_password()
    assert db.cfg_get("password_hash") == existing
