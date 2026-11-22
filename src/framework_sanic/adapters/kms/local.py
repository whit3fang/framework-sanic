"""Local KeyProvider for development: wraps DEKs with AES-GCM under a configured master key."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LocalKeyProvider:
    def __init__(self, master_key_b64: str) -> None:
        raw = base64.b64decode(master_key_b64)
        # Accept any length; derive a 256-bit key.
        self._key = hashlib.sha256(raw).digest()
        self.key_id = "local:" + hashlib.sha256(self._key).hexdigest()[:8]

    async def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key).encrypt(nonce, dek, self.key_id.encode())

    async def unwrap(self, wrapped: bytes) -> bytes:
        return AESGCM(self._key).decrypt(wrapped[:12], wrapped[12:], self.key_id.encode())
