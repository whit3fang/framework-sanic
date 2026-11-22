"""TokenStore over any Repository, with envelope encryption of every token at rest."""

from __future__ import annotations

from typing import Literal

from framework_sanic.core.domain.entities import DialpadCredential, ExternalCredential
from framework_sanic.core.domain.records import Record
from framework_sanic.core.ports.platform import OAuthTokens
from framework_sanic.core.ports.repository import Repository
from framework_sanic.core.ports.runtime import Clock
from framework_sanic.core.services.crypto import EnvelopeCipher


class RepositoryTokenStore:
    def __init__(self, repo: Repository, cipher: EnvelopeCipher, clock: Clock) -> None:
        self._repo = repo
        self._cipher = cipher
        self._clock = clock

    def _expires_at(self, tokens: OAuthTokens) -> float | None:
        return self._clock.now() + tokens.expires_in if tokens.expires_in else None

    # --- Dialpad ----------------------------------------------------------------------------
    async def list_dialpad(self, company_id: str) -> list[DialpadCredential]:
        return [
            DialpadCredential.from_record(r)
            async for r in self._repo.query(DialpadCredential.KIND, company_id=company_id)
        ]

    async def save_dialpad(
        self, company_id: str, credential_id: str, admin_user_id: str, tokens: OAuthTokens
    ) -> DialpadCredential:
        aad = f"dialpad:{company_id}:{credential_id}"
        access = await self._cipher.encrypt_str(tokens.access_token, aad)
        refresh = await self._cipher.encrypt_str(tokens.refresh_token, aad) if tokens.refresh_token else None
        now = self._clock.now()

        def merge(cur: Record | None) -> Record | None:
            existing = DialpadCredential.from_record(cur) if cur else None
            return DialpadCredential(
                company_id=company_id,
                credential_id=credential_id,
                admin_user_id=admin_user_id,
                priority=existing.priority if existing else now,  # first connected stays primary
                access_token_enc=access,
                refresh_token_enc=refresh,
                token_expires_at=self._expires_at(tokens),
                scopes=tokens.scope,
                status="ok",
                version=(existing.version + 1) if existing else 1,
            ).to_record()

        await self._repo.upsert(DialpadCredential.KIND, (company_id, credential_id), merge)
        rec = await self._repo.get(DialpadCredential.KIND, (company_id, credential_id))
        assert rec is not None
        return DialpadCredential.from_record(rec)

    async def dialpad_tokens(self, cred: DialpadCredential) -> OAuthTokens:
        aad = f"dialpad:{cred.company_id}:{cred.credential_id}"
        return OAuthTokens(
            access_token=await self._cipher.decrypt_str(cred.access_token_enc, aad),
            refresh_token=await self._cipher.decrypt_str(cred.refresh_token_enc, aad)
            if cred.refresh_token_enc
            else None,
            scope=cred.scopes,
        )

    async def cas_update_dialpad(
        self,
        cred: DialpadCredential,
        *,
        tokens: OAuthTokens | None = None,
        status: Literal["ok", "bad"] | None = None,
    ) -> DialpadCredential | None:
        aad = f"dialpad:{cred.company_id}:{cred.credential_id}"
        update: dict[str, object] = {}
        if tokens is not None:
            update["access_token_enc"] = await self._cipher.encrypt_str(tokens.access_token, aad)
            if tokens.refresh_token:
                update["refresh_token_enc"] = await self._cipher.encrypt_str(tokens.refresh_token, aad)
            update["token_expires_at"] = self._expires_at(tokens)
            update["status"] = "ok"
        if status is not None:
            update["status"] = status
        won = False

        def merge(cur: Record | None) -> Record | None:
            nonlocal won
            if cur is None or int(cur.get("version", 0)) != cred.version:
                return None
            won = True
            existing = DialpadCredential.from_record(cur)
            return existing.model_copy(update={**update, "version": cred.version + 1}).to_record()

        await self._repo.upsert(DialpadCredential.KIND, cred.key(), merge)
        if not won:
            return None
        rec = await self._repo.get(DialpadCredential.KIND, cred.key())
        return DialpadCredential.from_record(rec) if rec else None

    # --- External ---------------------------------------------------------------------------
    async def get_external(
        self, company_id: str, scope: Literal["user", "company"], user_id: str = ""
    ) -> ExternalCredential | None:
        rec = await self._repo.get(
            ExternalCredential.KIND, (company_id, scope, user_id if scope == "user" else "")
        )
        return ExternalCredential.from_record(rec) if rec else None

    async def save_external(
        self,
        company_id: str,
        scope: Literal["user", "company"],
        user_id: str,
        connector: str,
        tokens: OAuthTokens,
        external_user_id: str | None = None,
    ) -> ExternalCredential:
        uid = user_id if scope == "user" else ""
        blob = await self._cipher.encrypt_str(
            tokens.model_dump_json(), f"external:{company_id}:{scope}:{uid}"
        )

        def merge(cur: Record | None) -> Record | None:
            existing = ExternalCredential.from_record(cur) if cur else None
            return ExternalCredential(
                company_id=company_id,
                scope=scope,
                user_id=uid,
                connector=connector,
                tokens_enc=blob,
                token_expires_at=self._expires_at(tokens),
                status="ok",
                version=(existing.version + 1) if existing else 1,
                external_user_id=external_user_id,
            ).to_record()

        await self._repo.upsert(ExternalCredential.KIND, (company_id, scope, uid), merge)
        cred = await self.get_external(company_id, scope, uid)
        assert cred is not None
        return cred

    async def external_tokens(self, cred: ExternalCredential) -> OAuthTokens:
        raw = await self._cipher.decrypt_str(
            cred.tokens_enc, f"external:{cred.company_id}:{cred.scope}:{cred.user_id}"
        )
        return OAuthTokens.model_validate_json(raw)

    async def cas_update_external(
        self,
        cred: ExternalCredential,
        *,
        tokens: OAuthTokens | None = None,
        status: Literal["ok", "bad"] | None = None,
    ) -> ExternalCredential | None:
        update: dict[str, object] = {}
        if tokens is not None:
            update["tokens_enc"] = await self._cipher.encrypt_str(
                tokens.model_dump_json(), f"external:{cred.company_id}:{cred.scope}:{cred.user_id}"
            )
            update["token_expires_at"] = self._expires_at(tokens)
            update["status"] = "ok"
        if status is not None:
            update["status"] = status
        won = False

        def merge(cur: Record | None) -> Record | None:
            nonlocal won
            if cur is None or int(cur.get("version", 0)) != cred.version:
                return None
            won = True
            existing = ExternalCredential.from_record(cur)
            return existing.model_copy(update={**update, "version": cred.version + 1}).to_record()

        await self._repo.upsert(ExternalCredential.KIND, cred.key(), merge)
        if not won:
            return None
        return await self.get_external(cred.company_id, cred.scope, cred.user_id)
