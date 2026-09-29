"""Credential encryption — envelope encryption for broker secrets.

Broker logins/passwords/servers are encrypted at rest with a master key
(secret 12). The current scheme is a versioned, authenticated envelope so the
master key can be rotated later by re-wrapping rather than re-encrypting every
row (Phase 5: "key rotation ready").

Layout:  v1:<key_version>:<nonce_b64>:<ciphertext_b64>
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_PREFIX = "v1"


def _master_key() -> bytes:
    raw = os.getenv("CREDENTIAL_MASTER_KEY", "")
    if not raw:
        raise RuntimeError("CREDENTIAL_MASTER_KEY is not set")
    try:
        key = base64.b64decode(raw)
    except Exception as exc:
        raise RuntimeError("CREDENTIAL_MASTER_KEY must be base64") from exc
    if len(key) not in (16, 24, 32):
        raise RuntimeError(
            f"CREDENTIAL_MASTER_KEY must decode to 16/24/32 bytes, got {len(key)}"
        )
    return key


def _data_key(key_version: str) -> bytes:
    """Derive the per-version data key from the master key."""
    return hashlib.sha256(
        f"{ENVELOPE_PREFIX}:{key_version}".encode() + _master_key()
    ).digest()


def encrypt(plaintext: str, *, key_version: str = "v1") -> str:
    if plaintext is None:
        raise ValueError("Cannot encrypt None")
    key_version = os.getenv("CREDENTIAL_KEY_VERSION", key_version)
    key = _data_key(key_version)
    nonce = os.urandom(12)
    aad = key_version.encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    return ":".join([
        ENVELOPE_PREFIX,
        key_version,
        base64.b64encode(nonce).decode(),
        base64.b64encode(ciphertext).decode(),
    ])


def decrypt(envelope: str) -> str:
    parts = envelope.split(":")
    if len(parts) != 4 or parts[0] != ENVELOPE_PREFIX:
        raise ValueError("Malformed credential envelope")
    _, key_version, nonce_b64, ct_b64 = parts
    key = _data_key(key_version)
    nonce = base64.b64decode(nonce_b64)
    aad = key_version.encode()
    plaintext = AESGCM(key).decrypt(nonce, base64.b64decode(ct_b64), aad)
    return plaintext.decode()


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(f"{ENVELOPE_PREFIX}:") and value.count(":") == 3


def rewrap(ciphertext: str, new_key_version: str) -> str:
    """Re-encrypt an existing envelope under a new key version. This is what
    makes master-key rotation possible without touching every row twice."""
    plaintext = decrypt(ciphertext)
    return encrypt(plaintext, key_version=new_key_version)


def generate_master_key() -> str:
    """Helper for operators: prints a fresh base64 master key."""
    return base64.b64encode(os.urandom(32)).decode()
