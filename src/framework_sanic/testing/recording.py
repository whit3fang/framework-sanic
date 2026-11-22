"""Record real API responses once, replay them in CI (a "cassette").

Wrap the connector's HTTP client transport::

    transport = CassetteTransport("tests/cassettes/search.json")   # replay (default)
    connector = AcmeConnector(http=httpx.AsyncClient(transport=transport), ...)

Record against a sandbox with ``CASSETTE_MODE=record`` (and real credentials in the environment);
commit the cassette. Replays match requests by method, URL and body, in order, and fail loudly on
any request that was not recorded. Credentials are never written: only the method, URL, body and
the response status, content type and body are stored. Check recorded bodies for personal data
before committing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import httpx

Mode = Literal["replay", "record"]
KEPT_RESPONSE_HEADERS = {"content-type", "retry-after"}


class CassetteMiss(AssertionError):
    pass


def _request_key(request: httpx.Request) -> dict[str, Any]:
    body = request.content.decode("utf-8", errors="replace") if request.content else ""
    try:
        parsed: Any = json.loads(body) if body else None
    except ValueError:
        parsed = body
    return {"method": request.method, "url": str(request.url), "body": parsed}


class CassetteTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        path: str | Path,
        *,
        mode: Mode | None = None,
        real: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.path = Path(path)
        env_mode = os.environ.get("CASSETTE_MODE", "replay")
        self.mode: Mode = mode or ("record" if env_mode == "record" else "replay")
        self.real = real or httpx.AsyncHTTPTransport()
        self.interactions: list[dict[str, Any]] = []
        self._cursor = 0
        if self.mode == "replay":
            if not self.path.exists():
                raise CassetteMiss(f"no cassette at {self.path}: record it with CASSETTE_MODE=record")
            self.interactions = json.loads(self.path.read_text())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = _request_key(request)
        if self.mode == "record":
            response = await self.real.handle_async_request(request)
            await response.aread()
            headers = {k: v for k, v in response.headers.items() if k.lower() in KEPT_RESPONSE_HEADERS}
            text = response.content.decode("utf-8", errors="replace")
            self.interactions.append(
                {
                    "request": key,
                    "response": {"status": response.status_code, "headers": headers, "body": text},
                }
            )
            self.save()
            return httpx.Response(response.status_code, headers=headers, content=response.content)
        for i in range(self._cursor, len(self.interactions)):
            if self.interactions[i]["request"] == key:
                self._cursor = i + 1
                r = self.interactions[i]["response"]
                return httpx.Response(r["status"], headers=r["headers"], content=r["body"].encode())
        raise CassetteMiss(f"request not in {self.path}: {key['method']} {key['url']}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.interactions, indent=2, sort_keys=True) + "\n")

    async def aclose(self) -> None:
        if self.mode == "record":
            await self.real.aclose()
