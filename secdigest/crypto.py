"""Symmetric encryption for at-rest secrets, keyed off SECRET_KEY."""
import base64
import hashlib
import hmac
import os

from secdigest import config

_PREFIX = "enc:v1:"


def _key() -> bytes:
    return hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest()


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns 'enc:v1:<base64>' or '' for empty input."""
    if not plaintext:
        return ""
    iv = os.urandom(16)
    key = _key()
    # Simple stream-style: HMAC-DRBG-ish using HMAC(key, iv || counter)
    pt = plaintext.encode("utf-8")
    keystream = b""
    counter = 0
    while len(keystream) < len(pt):
        counter += 1
        keystream += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
    ct = bytes(p ^ k for p, k in zip(pt, keystream[: len(pt)]))
    mac = hmac.new(key, iv + ct, hashlib.sha256).digest()
    blob = iv + mac + ct
    return _PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt a value. Returns plaintext as-is if not encrypted (legacy migration)."""
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext
    try:
        blob = base64.urlsafe_b64decode(value[len(_PREFIX):].encode("ascii"))
        iv, mac, ct = blob[:16], blob[16:48], blob[48:]
        key = _key()
        expected_mac = hmac.new(key, iv + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            return ""
        keystream = b""
        counter = 0
        while len(keystream) < len(ct):
            counter += 1
            keystream += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        pt = bytes(c ^ k for c, k in zip(ct, keystream[: len(ct)]))
        return pt.decode("utf-8")
    except Exception:
        return ""
