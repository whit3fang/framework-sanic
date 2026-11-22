"""Envelope encryption over a KeyProvider.

A data-encryption key (DEK) is generated locally, wrapped by the KeyProvider (GCP KMS in prod)
and reused for ``dek_ttl_seconds``. That keeps the webhook accept path free of per-message KMS
calls, so SMS text can be encrypted even while Redis and the database are down.

Format: b"E1" | u16 len(wrapped) | wrapped DEK | 12-byte nonce | AES-256-GCM ciphertext
"""

from __future__ import annotations

import asyncio
import base64
import os
import struct
import time
from collections import OrderedDict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..ports.security import KeyProvider

_MAGIC = b"E1"


class EnvelopeCipher:
    def __init__(
        self, provider: KeyProvider, *, dek_ttl_seconds: float = 3600.0, cache_size: int = 256
    ) -> None:
        self._provider = provider
        self._ttl = dek_ttl_seconds
        self._current: tuple[bytes, bytes, float] | None = None  # (dek, wrapped, created)
        self._unwrapped: OrderedDict[bytes, bytes] = OrderedDict()
        self._cache_size = cache_size
        self._lock = asyncio.Lock()

    async def _dek(self) -> tuple[bytes, bytes]:
        cur = self._current
        if cur and time.monotonic() - cur[2] < self._ttl:
            return cur[0], cur[1]
        async with self._lock:
            cur = self._current
            if cur and time.monotonic() - cur[2] < self._ttl:
                return cur[0], cur[1]
            dek = AESGCM.generate_key(bit_length=256)
            wrapped = await self._provider.wrap(dek)
            self._current = (dek, wrapped, time.monotonic())
            self._remember(wrapped, dek)
            return dek, wrapped

    def _remember(self, wrapped: bytes, dek: bytes) -> None:
        self._unwrapped[wrapped] = dek
        self._unwrapped.move_to_end(wrapped)
        while len(self._unwrapped) > self._cache_size:
            self._unwrapped.popitem(last=False)

    async def warm(self) -> None:
        """Create the first DEK at startup so the accept path never waits on KMS."""
        await self._dek()

    async def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        dek, wrapped = await self._dek()
        nonce = os.urandom(12)
        ct = AESGCM(dek).encrypt(nonce, plaintext, aad or None)
        return _MAGIC + struct.pack(">H", len(wrapped)) + wrapped + nonce + ct

    async def decrypt(self, blob: bytes, aad: bytes = b"") -> bytes:
        if blob[:2] != _MAGIC:
            raise ValueError("unknown ciphertext format")
        (n,) = struct.unpack(">H", blob[2:4])
        wrapped = blob[4 : 4 + n]
        nonce = blob[4 + n : 16 + n]
        ct = blob[16 + n :]
        dek = self._unwrapped.get(wrapped)
        if dek is None:
            dek = await self._provider.unwrap(wrapped)
            self._remember(wrapped, dek)
        return AESGCM(dek).decrypt(nonce, ct, aad or None)

    async def encrypt_str(self, text: str, aad: str = "") -> str:
        return base64.urlsafe_b64encode(await self.encrypt(text.encode(), aad.encode())).decode()

    async def decrypt_str(self, token: str, aad: str = "") -> str:
        return (await self.decrypt(base64.urlsafe_b64decode(token.encode()), aad.encode())).decode()
