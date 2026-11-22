"""GCP Cloud KMS KeyProvider (production). Requires the ``gcp`` extra."""

from __future__ import annotations

import asyncio
from typing import Any


class GcpKmsKeyProvider:
    def __init__(self, key_name: str, client: Any = None) -> None:
        if client is None:
            from google.cloud import kms

            client = kms.KeyManagementServiceClient()
        self._client = client
        self.key_id = key_name

    async def wrap(self, dek: bytes) -> bytes:
        resp = await asyncio.to_thread(
            lambda: self._client.encrypt(request={"name": self.key_id, "plaintext": dek})
        )
        return bytes(resp.ciphertext)

    async def unwrap(self, wrapped: bytes) -> bytes:
        resp = await asyncio.to_thread(
            lambda: self._client.decrypt(request={"name": self.key_id, "ciphertext": wrapped})
        )
        return bytes(resp.plaintext)
