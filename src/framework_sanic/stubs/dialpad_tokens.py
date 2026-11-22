"""Dev ``id_token`` minting for tests and the fake Dialpad harness.

Produces the format ``adapters/dialpad/id_token.DialpadIdTokenVerifier`` currently assumes
(VERIFY: see docs/open-questions.md #2). Keep the two in step if the real format changes.
"""

from __future__ import annotations

import time

import jwt


def make_dev_id_token(secret: str, *, company_id: str, user_id: str, is_admin: bool, ttl: int = 3600) -> str:
    """HS256 JWT with company_id, user_id, is_admin and exp, signed with ``secret``."""
    return jwt.encode(
        {"company_id": company_id, "user_id": user_id, "is_admin": is_admin, "exp": int(time.time()) + ttl},
        secret,
        algorithm="HS256",
    )
