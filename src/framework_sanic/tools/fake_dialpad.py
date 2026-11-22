"""Fake Dialpad driver for the local harness.

Plays Dialpad's role against a running service started with PLATFORM_ADAPTER=fake:
admin OAuth connect, external connect, and signed webhook replay (including duplicated and
shuffled sequences), then verifies the fake call listener (``fake.calls``) received the call.

  framework-sanic replay e2e --base http://localhost:8000
  framework-sanic replay replay-call --call-id 42 --shuffle --duplicates 3
  framework-sanic replay replay-file events.json --shuffle
  framework-sanic replay sms --text "hi"

Signing uses the same derivation as the service (HMAC of the company id under
WEBHOOK_SECRET_KEY), so both must share that env var.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import time
from typing import Any

import httpx
import jwt

COMPANY = "c1"
ADMIN = "c1-u0"


def webhook_secret(company_id: str) -> str:
    master = os.environ.get("WEBHOOK_SECRET_KEY", "dev-webhook-master-key").encode()
    return hmac.new(master, f"dialpad-webhook:{company_id}".encode(), hashlib.sha256).hexdigest()


def id_token(company_id: str, user_id: str, is_admin: bool) -> str:
    secret = os.environ.get("DIALPAD_CLIENT_SECRET", "dev-client-secret-change-me-0123456789")
    return jwt.encode(
        {"company_id": company_id, "user_id": user_id, "is_admin": is_admin, "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )


def call_sequence(call_id: str, *, number: str = "+14155550100", user: str = "c1-u1") -> list[dict[str, Any]]:
    t0 = int(time.time() * 1000)
    base = {
        "call_id": call_id,
        "direction": "inbound",
        "external_number": number,
        "internal_number": "+14155550199",
        "target": {"id": user, "type": "user", "name": "User 1"},
        "contact": {"phone": number, "name": "Caller"},
        "date_started": t0,
    }
    return [
        {**base, "state": "calling", "event_timestamp": t0},
        {**base, "state": "ringing", "event_timestamp": t0 + 500},
        {**base, "state": "connected", "event_timestamp": t0 + 3000, "date_connected": t0 + 3000},
        {
            **base,
            "state": "hangup",
            "event_timestamp": t0 + 95_000,
            "date_connected": t0 + 3000,
            "date_ended": t0 + 95_000,
            "duration": 92_000.0,
        },
        {
            **base,
            "state": "recording",
            "event_timestamp": t0 + 96_000,
            "recording_details": [{"url": f"https://dialpad.example/recordings/{call_id}"}],
        },
    ]


def mutate(
    events: list[dict[str, Any]], *, shuffle: bool, duplicates: int, seed: int | None
) -> list[dict[str, Any]]:
    rnd = random.Random(seed)
    out = list(events) + [rnd.choice(events) for _ in range(duplicates)]
    if shuffle:
        rnd.shuffle(out)
    return out


class Driver:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=10.0)

    def connect_admin(self) -> None:
        r = self.http.get("/oauth/dialpad/start", follow_redirects=True)
        r.raise_for_status()
        print("admin connect:", r.status_code)

    def connect_external(self, company_id: str = COMPANY, user_id: str = ADMIN) -> None:
        headers = {"X-Dialpad-Id-Token": id_token(company_id, user_id, True)}
        r = self.http.get("/oauth/external/start", params={"scope": "company"}, headers=headers)
        r.raise_for_status()
        url = r.json()["authorize_url"]
        r = self.http.get(url.replace(self.base, ""), follow_redirects=True)
        r.raise_for_status()
        print("external connect:", r.status_code)

    def session_state(self, company_id: str = COMPANY, user_id: str = ADMIN) -> str:
        r = self.http.get("/session", headers={"X-Dialpad-Id-Token": id_token(company_id, user_id, True)})
        r.raise_for_status()
        return str(r.json()["state"])

    def seed_contact(self, name: str, phone: str) -> None:
        self.http.post("/dev/fake-connector/contacts", json={"name": name, "phone": phone}).raise_for_status()

    def send(self, company_id: str, events: list[dict[str, Any]]) -> None:
        secret = webhook_secret(company_id)
        for e in events:
            body = jwt.encode(e, secret, algorithm="HS256")
            r = self.http.post(
                f"/webhooks/dialpad/{company_id}/events",
                content=body,
                headers={"Content-Type": "application/jwt"},
            )
            print(f"  webhook {e.get('state', 'sms')}@{e.get('event_timestamp', '')} -> {r.status_code}")
            if r.status_code >= 300:
                raise SystemExit(f"webhook rejected: {r.status_code} {r.text}")

    def records(self) -> dict[str, Any]:
        r = self.http.get("/dev/fake-connector/records")
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    def wait_for_call(self, company_id: str, call_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """Until the fake listener has received every state of the call and its recording."""
        wanted = {e["state"] for e in call_sequence(call_id)}
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = [
                r
                for r in self.records()["calls"]
                if r["company_id"] == company_id and r["call_id"] == call_id
            ]
            if matches and wanted <= set(matches[0]["states"]) and matches[0]["recording_urls"]:
                record: dict[str, Any] = matches[0]
                return record
            time.sleep(0.5)
        raise SystemExit(f"call {call_id} was not fully delivered to the fake listener within {timeout}s")


def sms_event(
    sms_id: int, *, number: str = "+14155550100", user: str = "c1-u1", text: str = "hello"
) -> dict[str, Any]:
    return {
        "id": sms_id,
        "created_date": int(time.time() * 1000),
        "direction": "inbound",
        "from_number": number,
        "to_number": ["+14155550199"],
        "target": {"id": user, "type": "user"},
        "contact": {"phone": number},
        "message_status": "sent",
        "text": text,
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="framework-sanic replay")
    p.add_argument("--base", default=os.environ.get("APP_BASE_URL", "http://localhost:8000"))
    sub = p.add_subparsers(dest="cmd", required=True)
    e2e = sub.add_parser(
        "e2e", help="connect, replay a shuffled/duplicated call, verify the listener got it all"
    )
    e2e.add_argument("--call-id", default=str(int(time.time())))
    rc = sub.add_parser("replay-call")
    rc.add_argument("--company", default=COMPANY)
    rc.add_argument("--call-id", default=str(int(time.time())))
    rc.add_argument("--shuffle", action="store_true")
    rc.add_argument("--duplicates", type=int, default=0)
    rc.add_argument("--seed", type=int)
    sd = sub.add_parser("seed", help="connect admin + external account, seed a contact, log one call")
    sd.add_argument("--call-id", default=str(int(time.time())))
    sd.add_argument("--phone", default="+14155550100")
    sd.add_argument("--name", default="Ada Lovelace")
    tk = sub.add_parser("dev-token", help="print a dev id_token for the iframe URL")
    tk.add_argument("--company", default=COMPANY)
    tk.add_argument("--user", default=ADMIN)
    tk.add_argument("--no-admin", action="store_true")
    sm = sub.add_parser("sms", help="send one inbound SMS event")
    sm.add_argument("--company", default=COMPANY)
    sm.add_argument("--sms-id", type=int, default=int(time.time()))
    sm.add_argument("--text", default="hello from the fake Dialpad")
    rf = sub.add_parser("replay-file")
    rf.add_argument("path")
    rf.add_argument("--company", default=COMPANY)
    rf.add_argument("--shuffle", action="store_true")
    rf.add_argument("--duplicates", type=int, default=0)
    args = p.parse_args(argv)

    if args.cmd == "dev-token":
        print(id_token(args.company, args.user, not args.no_admin))
        return

    d = Driver(args.base)
    for _ in range(60):  # wait for the service
        try:
            if d.http.get("/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)

    if args.cmd == "e2e":
        d.connect_admin()
        d.connect_external()
        state = d.session_state()
        print("session state:", state)
        if state != "ready":
            raise SystemExit(f"expected state=ready, got {state}")
        d.seed_contact("Ada Lovelace", "+14155550100")
        events = mutate(call_sequence(args.call_id), shuffle=True, duplicates=3, seed=7)
        print(f"replaying {len(events)} events (shuffled, 3 duplicates) for call {args.call_id}")
        d.send(COMPANY, events)
        record = d.wait_for_call(COMPANY, args.call_id)
        print("received by fake.calls:", json.dumps(record, indent=2))
        print("E2E OK")
    elif args.cmd == "seed":
        d.connect_admin()
        d.connect_external()
        known = {c["properties"].get("phone") for c in d.records()["contacts"]}
        if args.phone not in known:
            d.seed_contact(args.name, args.phone)
        d.send(COMPANY, call_sequence(args.call_id, number=args.phone))
        d.wait_for_call(COMPANY, args.call_id)
        print(f"seeded: company={COMPANY} contact={args.name} ({args.phone}) call={args.call_id}")
    elif args.cmd == "replay-call":
        d.send(
            args.company,
            mutate(
                call_sequence(args.call_id), shuffle=args.shuffle, duplicates=args.duplicates, seed=args.seed
            ),
        )
    elif args.cmd == "sms":
        d.send(args.company, [sms_event(args.sms_id, text=args.text)])
    elif args.cmd == "replay-file":
        with open(args.path) as fh:
            events = json.load(fh)
        d.send(args.company, mutate(events, shuffle=args.shuffle, duplicates=args.duplicates, seed=None))


if __name__ == "__main__":
    main()
