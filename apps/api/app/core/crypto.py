"""Credential encryption at rest.

Broker logins and passwords are encrypted with Fernet (AES-128-CBC +
HMAC-SHA256) before they reach PostgreSQL, and the master key never leaves the
API process. Each ciphertext records the key version so a key can be rotated
without a full re-encryption pass.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

logger = logging.getLogger(__name__)

PLACEHOLDER = ""


def _key_from_secret(secret: str) -> bytes:
    """Derive a 32-byte urlsafe base64 key from an arbitrary master secret.

    Users supply a passphrase, not a Fernet key, so hash it to the length
    Fernet requires. A value that is already a valid Fernet key is used as-is.
    """
    try:
        raw = base64.urlsafe_b64decode(secret.encode())
        if len(raw) == 32:
            return secret.encode()
    except (ValueError, binascii.Error):
        pass
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_key_from_secret(get_settings().credential_master_key))


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return PLACEHOLDER
    version = get_settings().credential_version_number
    return f"v{version}:{_fernet().encrypt(plaintext.encode()).decode()}"


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    _, _, payload = ciphertext.partition(":")
    if not payload:
        # Unprefixed value: written before versioning was introduced.
        payload = ciphertext
    try:
        return _fernet().decrypt(payload.encode()).decode()
    except InvalidToken:
        logger.error("Credential could not be decrypted; key mismatch or tampering")
        return ""
