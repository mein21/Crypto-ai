"""Symmetric encryption for Bybit API keys.

Master key (`AUTOTRADE_MASTER_KEY`) is a base64-encoded 32-byte Fernet key.
We never write secrets to disk in plaintext: even the SQLite blob is
ciphertext, decrypted only in-memory when a Bybit call is about to be made.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken


def generate_master_key() -> str:
    """Generate a new master key. Useful for fly secret rotations."""
    return Fernet.generate_key().decode("ascii")


def _coerce_master_key(raw: str) -> bytes:
    if not raw:
        raise RuntimeError(
            "AUTOTRADE_MASTER_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and store it as a "
            "Fly.io secret before starting the worker."
        )
    raw = raw.strip()
    try:
        # validate by attempting to construct Fernet
        Fernet(raw.encode("ascii"))
        return raw.encode("ascii")
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            "AUTOTRADE_MASTER_KEY must be a base64-urlsafe Fernet key "
            "(32 bytes). Run the helper above to mint a fresh one."
        ) from e


class KeyVault:
    def __init__(self, master_key: str) -> None:
        self._fernet = Fernet(_coerce_master_key(master_key))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as e:
            raise RuntimeError(
                "Failed to decrypt Bybit credentials — master key likely "
                "rotated since the secret was saved. Re-enter API keys."
            ) from e
