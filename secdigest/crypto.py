"""Symmetric encryption for at-rest secrets, keyed off SECRET_KEY.

Why this exists: things like SMTP passwords and the Anthropic API key are
written into the SQLite database via the settings page. We don't want
them sitting on disk in plaintext where a stolen DB file equals stolen
credentials, so we wrap them with a small authenticated-encryption layer
before they hit the database, and unwrap on read.

Format of an encrypted value (after b64 decode of the part after
``enc:v1:``):

    [ 16-byte IV ][ 32-byte HMAC ][ ciphertext ... ]

Crypto choices, in plain English:
- The encryption key is the SHA-256 of ``config.SECRET_KEY`` — a single
  app-wide secret. Rotating SECRET_KEY invalidates every existing blob.
- We don't have AES handy in the stdlib, so we build a stream cipher
  out of HMAC-SHA256: HMAC(key, IV || counter) generates 32 bytes of
  pseudorandom keystream per counter step, which is then XOR'd against
  the plaintext. This is the "encrypt-then-MAC" pattern.
- The MAC covers IV + ciphertext, so any tampering causes ``decrypt``
  to return ``""`` rather than corrupted plaintext.

Note: this is a from-scratch construction tuned to avoid extra deps.
For new code, prefer ``cryptography``'s Fernet or AES-GCM.
"""
import base64
import hashlib
import hmac
import os

from secdigest import config

# Versioned prefix so we can recognise our own ciphertexts and migrate
# format later (e.g. ``enc:v2:``) without breaking decrypt of old rows.
_PREFIX = "enc:v1:"


def _key() -> bytes:
    # SECRET_KEY is human-typed and arbitrary length; SHA-256 stretches
    # it into a fixed 32-byte HMAC key.
    return hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest()


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns 'enc:v1:<base64>' or '' for empty input."""
    # Treat empty strings as "nothing to protect" — keeps optional
    # settings (no SMTP password set, etc.) from producing noise blobs.
    if not plaintext:
        return ""
    # Fresh random IV per encryption so the same plaintext encrypts to
    # different ciphertexts each time. Reusing an IV with this construction
    # would leak XOR of plaintexts.
    iv = os.urandom(16)
    key = _key()
    # Build the keystream lazily: each round produces 32 bytes via
    # HMAC(key, iv || counter). Counter starts at 1 and is encoded
    # big-endian so different platforms agree byte-for-byte.
    pt = plaintext.encode("utf-8")
    keystream = b""
    counter = 0
    while len(keystream) < len(pt):
        counter += 1
        keystream += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
    # XOR plaintext with the (truncated) keystream to get ciphertext.
    ct = bytes(p ^ k for p, k in zip(pt, keystream[: len(pt)]))
    # Authenticate IV + ciphertext so we can detect any tampering on read.
    mac = hmac.new(key, iv + ct, hashlib.sha256).digest()
    # On-disk layout is one contiguous blob; ``decrypt`` slices it back apart.
    blob = iv + mac + ct
    # urlsafe_b64 keeps the value friendly to URLs/headers/JSON if it ever leaks out.
    return _PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt(value: str) -> str:
    """Decrypt a value. Returns plaintext as-is if not encrypted (legacy migration)."""
    if not value:
        return ""
    # Anything we wrote ourselves carries the ``enc:v1:`` prefix. Values
    # that don't are legacy plaintext from before encryption was added,
    # and we let them flow through so old rows keep working until they're
    # rewritten on the next save.
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext
    try:
        blob = base64.urlsafe_b64decode(value[len(_PREFIX):].encode("ascii"))
        # Slice the layout back into its three parts. Lengths are fixed
        # (16 + 32) so the rest is ciphertext.
        iv, mac, ct = blob[:16], blob[16:48], blob[48:]
        key = _key()
        expected_mac = hmac.new(key, iv + ct, hashlib.sha256).digest()
        # Constant-time compare — never use ``==`` on MACs, it short-circuits
        # on the first mismatched byte and leaks timing info.
        if not hmac.compare_digest(mac, expected_mac):
            return ""
        # Regenerate the same keystream we used during encryption, then XOR
        # again to recover the plaintext (XOR is its own inverse).
        keystream = b""
        counter = 0
        while len(keystream) < len(ct):
            counter += 1
            keystream += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        pt = bytes(c ^ k for c, k in zip(ct, keystream[: len(ct)]))
        return pt.decode("utf-8")
    except Exception:
        # Any failure (bad base64, truncated blob, wrong UTF-8, ...) is
        # treated as "no value" rather than crashing the caller — settings
        # pages should keep loading even if one row is corrupt.
        return ""
