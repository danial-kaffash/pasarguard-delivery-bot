"""Fernet encryption for sensitive data at rest (panel passwords).

Uses a key from the ``DB_ENCRYPTION_KEY`` environment variable.
If the key is not set, falls back to a warning and no-op passthrough
so the bot can still run in development without encryption.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_fernet = None
_loaded = False


def _load_fernet():
    """Lazily load the Fernet instance from the environment."""
    global _fernet, _loaded
    if _loaded:
        return
    _loaded = True
    key = os.environ.get("DB_ENCRYPTION_KEY", "").strip()
    if not key:
        logger.warning(
            "DB_ENCRYPTION_KEY is not set — panel passwords will be stored in PLAINTEXT. "
            "Generate a key: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
        return
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        logger.info("DB_ENCRYPTION_KEY loaded — panel passwords will be encrypted.")
    except Exception:
        logger.exception("DB_ENCRYPTION_KEY is invalid — panel passwords will be stored in PLAINTEXT.")


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns ciphertext as a string (utf-8)."""
    _load_fernet()
    if _fernet is None:
        return plaintext  # no-op fallback
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a string. Returns plaintext."""
    _load_fernet()
    if _fernet is None:
        return ciphertext  # no-op fallback
    try:
        return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception:
        # If decryption fails, the value might be plaintext (pre-encryption migration).
        logger.debug("Could not decrypt value — returning as-is (may be plaintext).")
        return ciphertext
