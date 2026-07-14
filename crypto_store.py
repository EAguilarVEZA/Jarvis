"""Encryption at rest for sensitive values (credentials, secrets, tokens).

Addresses HIPAA §164.312(a)(2)(iv) (encryption/decryption) and SOC 2 Confidentiality:
nothing sensitive should sit in plaintext JSON on disk. Values are encrypted with
Fernet (AES-128-CBC + HMAC-SHA256 authentication) and tagged with a version prefix so
plaintext (legacy) values keep working during migration.

Key management
--------------
The data key is taken from, in order:
  1. env JARVIS_SECRET_KEY  (recommended — supply from your KMS / secrets manager)
  2. a local key file `.jarvis_secret_key` (created with 0600 perms as a dev fallback)

For production/compliance the key MUST come from a managed KMS (env or mounted secret),
NOT the local file. `key_source()` reports which is in use so the compliance self-check
can flag a weak configuration.

If the `cryptography` package isn't installed, `available()` returns False and the app
keeps working with plaintext — but the compliance self-check will flag encryption as
NOT satisfied. Install `cryptography` to enable it.
"""
from __future__ import annotations

import os
import logging

log = logging.getLogger("crypto_store")

_DIR = os.path.dirname(os.path.abspath(__file__))
_KEY_FILE = os.path.join(_DIR, ".jarvis_secret_key")
PREFIX = "enc:1:"


def _fernet():
    from cryptography.fernet import Fernet  # raises if not installed
    return Fernet(_load_key())


def _load_key() -> bytes:
    k = os.getenv("JARVIS_SECRET_KEY", "").strip()
    if k:
        return k.encode()
    if os.path.exists(_KEY_FILE):
        return open(_KEY_FILE, "rb").read().strip()
    # dev fallback: generate + persist a key with restrictive permissions
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    try:
        with open(_KEY_FILE, "wb") as f:
            f.write(key)
        os.chmod(_KEY_FILE, 0o600)
    except Exception as e:
        log.warning(f"could not persist key file: {e}")
    return key


def key_source() -> str:
    """'env' (managed key, good), 'file' (dev fallback), or 'none'."""
    if os.getenv("JARVIS_SECRET_KEY", "").strip():
        return "env"
    if os.path.exists(_KEY_FILE):
        return "file"
    return "none"


def available() -> bool:
    """True if encryption at rest is usable (cryptography installed + key loadable)."""
    try:
        import cryptography.fernet  # noqa: F401
        _load_key()
        return True
    except Exception as e:
        log.info(f"encryption unavailable: {e}")
        return False


def is_encrypted(v) -> bool:
    return isinstance(v, str) and v.startswith(PREFIX)


def protect(v):
    """Encrypt a string value for storage. Idempotent (won't double-encrypt), and a
    no-op if encryption isn't available (value stays plaintext)."""
    if v is None or v == "" or not isinstance(v, str):
        return v
    if is_encrypted(v) or not available():
        return v
    try:
        return PREFIX + _fernet().encrypt(v.encode()).decode()
    except Exception as e:
        log.warning(f"protect failed: {e}")
        return v


def reveal(v):
    """Decrypt a stored value. Legacy plaintext (no prefix) passes through unchanged,
    so migration is seamless."""
    if not is_encrypted(v):
        return v
    if not available():
        return v
    try:
        return _fernet().decrypt(v[len(PREFIX):].encode()).decode()
    except Exception as e:
        log.warning(f"reveal failed: {e}")
        return v


def protect_dict(d: dict) -> dict:
    """Encrypt every string value in a flat dict (e.g. a credential's data blob)."""
    if not isinstance(d, dict):
        return d
    return {k: (protect(v) if isinstance(v, str) else v) for k, v in d.items()}


def reveal_dict(d: dict) -> dict:
    if not isinstance(d, dict):
        return d
    return {k: (reveal(v) if isinstance(v, str) else v) for k, v in d.items()}
