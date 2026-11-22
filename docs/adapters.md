# Adapters

Every external dependency sits behind a **port**, an interface in `src/framework_sanic/core/ports/`.
An **adapter** implements a port for one technology. Core services, features and HTTP handlers
only ever use the ports. `bootstrap.py` (the composition root) picks each adapter from config in
its `make_*` functions and hands it out through the `Services` container. Nothing else imports an
adapter.

```
services / features / handlers ──► ports (interfaces) ◄── adapters (memory, SQL, Datastore, Redis, ...)
                                                      ▲
                                    bootstrap.py picks one per port from config
```

Fakes of external systems (Dialpad, the CRM) are not adapters. They live in
`src/framework_sanic/stubs/` and production code may not import them (lint-enforced).

## Summary

| Port | Config | Adapters | Purpose |
|---|---|---|---|
| Repository | `STORAGE_ADAPTER` | memory, sqlalchemy, datastore, or your own (`module:factory`) | Source of truth for everything persistent |
| TokenStore | (always) | `storage/token_store.py` | Encrypted OAuth credentials, on top of any repository |
| Queue | `QUEUE_ADAPTER` | memory, pubsub | Durable buffer between webhooks and handlers |
| Cache | `CACHE_URL` | memory, redis | Fast path only: leases, dedupe, OAuth state, rate limits |
| KeyProvider | `KMS_KEY` | local, gcp_kms | Wraps the data keys used for encryption |
| ParentPlatform | `PLATFORM_ADAPTER` | `dialpad/platform.py` (fake in `stubs/dialpad.py`) | Dialpad OAuth, targets, webhooks, subscriptions |
| WebhookVerifier | (always) | `dialpad/webhook.py` | Verifies signed webhook bodies |
| IdTokenVerifier | (always) | `dialpad/id_token.py` | Verifies the iframe's per-user `id_token` |
| Scheduler | (always) | `scheduler/memory.py` | Periodic jobs |
| ChangeLogSource | (always) | `changelog/noop.py` | Dialpad change log (placeholder) |
| HTTP framework | (always) | `http/adapters/sanic.py` | Turns Sanic requests into framework-neutral ones |
| Connector | `CONNECTOR` | integration packages, e.g. `integrations/hubspot` (fake in `stubs/connector`) | The external system (CRM) |

## Storage: Repository (`STORAGE_ADAPTER`)

This port is the source of truth for companies, credentials, users, targets, subscriptions,
activities, notes, the outbox and the audit log.

**Contract** (documented in `core/ports/repository.py`, verified by the reusable suite in
`framework_sanic.testing.repository_contract`):
- an atomic `upsert(kind, key, merge_fn)` that reports created, updated, unchanged or ignored;
- small multi-record transactions (`unit_of_work`) that raise `Conflict` on concurrent changes;
- `get`, `delete`, and `query` by equality on top-level fields;
- expiry: a record whose `expires_at` has passed reads as absent;
- lifecycle methods: `setup(schema)` once at startup, `purge_expired(limit)` on a schedule, and
  `ping` and `close`.

| Adapter | How it works | Used for |
|---|---|---|
| `storage/memory` | Python dicts behind a lock. A `.down` switch simulates an outage. | Unit and integration tests |
| `storage/sqlalchemy` | One generic `records` table holding JSON, with a version column for compare-and-set. `setup` creates the table and the `(kind, company_id)` and `(kind, expires_at)` indexes. SQLite writes are serialised in-process. | Local dev (`scripts/dev.sh`, SQLite); works on PostgreSQL too |
| `storage/datastore` | Official `google-cloud-datastore` client, run in a thread pool because it's synchronous. Records are stored as a JSON blob plus indexed scalar fields; `_expire_at` backs native TTL. Indexes and TTL are deployed out of band: run `framework-sanic export-indexes`. | Intended production backend. ⚠️ Never run yet (no emulator in this environment). |
| Your own | `STORAGE_ADAPTER=package.module:factory`, where `factory(settings, clock)` returns a `Repository`. It must pass the contract suite; see the README's "Writing a storage adapter". | Deployments on other databases |

**Used by:**
- **Auth middleware:** loads the user on `id_token` login and checks the company's state.
- **`CompanyService`:** install, app state, parking, uninstall.
- **`TargetService`:** the target hierarchy and per-target logging overrides.
- **`SubscriptionReconciler` and `Sweeper`:** stored subscriptions, claimed with an atomic upsert.
- **`EntityMapService`:** `EntityMap` rows (Dialpad id → external record) for contacts and logged activities.
- **Token store:** encrypted credentials.
- **Outbox service and relay.**
- **`SyncGuards`:** reads the company's state to decide on parking.
- **Listeners (integration code):** whatever they store, e.g. HubSpot's `LoggedCall` fold and
  insert-once `LoggedSms`; the fake listeners' delivery records.
- **The platform feature:** scheduled `purge_expired`.
- **`/readyz`:** health ping.

### Token store (`storage/token_store.py`)

This implements the `TokenStore` port on top of whichever repository is configured. It encrypts
OAuth access and refresh tokens before they're stored, and updates them with compare-and-set so
concurrent refreshes can't overwrite each other.
- **Used by:** `DialpadCredentialService` and `ExternalCredentialService`.

## Queue (`QUEUE_ADAPTER`)

This is the durable buffer between the webhook endpoint and the feature handlers:
- messages are sharded by company and carry a lane (UI, live, backfill);
- consumers ack, nack with a delay (optionally without counting an attempt), or deadletter.

| Adapter | How it works | Used for |
|---|---|---|
| `queue/memory` | In-process shards, each holding lane → company → FIFO queue. Pulls take UI first, then live, then backfill, rotating across companies. Delayed retries wait on a timer heap. Test hooks: a `.published` log, a `.down` switch and `pending()`. Not durable, and only works within one process. | Tests, `scripts/dev.sh` (workers run inside the server) |
| `queue/pubsub` | Google Pub/Sub, one subscription per topic and shard. Works with the emulator in docker compose (`QUEUE_ADAPTER=pubsub`). | Production. ⚠️ Never run yet. |

**Used by:**
- **Webhook accept path:** publishes one message per feature handler. This is its only dependency.
- **Worker runtime:** pull, ack, retry and deadletter, with fairness and per-company caps.
- **`CompanyService`, admin routes, `Sweeper` and `ChangeLogWatcher`:** publish reconcile jobs.
- **Listeners:** their own follow-up jobs, e.g. HubSpot's `hubspot.sync_call` (via the outbox),
  `hubspot.sync_sms` and `hubspot.sync_note`.
- **`/readyz`:** ping and queue depths.

## Cache (`CACHE_URL`)

The cache is only ever a fast path, never the source of truth. Every caller keeps working when
it's down.

| Adapter | How it works | Used for |
|---|---|---|
| `cache/memory` | In-process dictionary with TTLs and a `.down` switch | Tests |
| `cache/redis` | Redis; token buckets are atomic via a Lua script | Local dev (`scripts/dev.sh` reuses, starts or containerises Redis), docker compose and production |

**Used by:**
- **`OAuthStateStore`:** single-use OAuth state and PKCE verifier.
- **`SessionService`:** session revocation list.
- **`RateLimiter`:** token buckets for the UI rate limit, the Dialpad API budget and the
  connector budget.
- **`DialpadCredentialService`:** token-refresh lease.
- **`SubscriptionReconciler`:** per-company reconcile lease.
- **`ContactService`:** short-lived contact lookup cache.
- **`SyncGuards`:** per-activity sync leases.
- **`TargetService`:** per-company lease so logging-config recomputes run one at a time.
- **`ContactService`:** per-caller lease while creating a contact for an unknown number.

Leases (`core/services/lease.py`) store a random token per holder and are released with
`delete_if`, so a holder whose lease expired never frees the one someone else took after it.
- **`/readyz`.**

## Key management: KeyProvider (`KMS_KEY`)

This wraps and unwraps the data keys behind `EnvelopeCipher`. Nothing uses it directly.

| Adapter | How it works | Used for |
|---|---|---|
| `kms/local` | AES-GCM with a master key from `LOCAL_MASTER_KEY` | Tests and local dev |
| `kms/gcp_kms` | Google Cloud KMS wraps the data keys | Production. ⚠️ Never run yet. |

**Used by (via `EnvelopeCipher`):**
- the accept path, which encrypts SMS text before queueing;
- SMS listeners (`await event.sms_text()`), which decrypt it just before sending;
- the token store, for every stored token;
- `startup()`, which warms the data-key cache.

## Dialpad

| Adapter | Port | What it does | Used by |
|---|---|---|---|
| `dialpad/platform.py` | ParentPlatform | Real Dialpad REST client: OAuth code exchange and refresh, identify company and admin, token check, list targets, manage webhooks and subscriptions | OAuth routes, `DialpadCredentialService`, `TargetService`, and `SubscriptionReconciler` via `PlatformGateway` (which applies the 1200/min budget) |
| `stubs/dialpad.py` (stub) | ParentPlatform | In-memory Dialpad. OAuth redirects straight back with a PKCE-bound code; it serves a sample company and records subscriptions. Selected with `PLATFORM_ADAPTER=fake`. | Tests, `framework-sanic dev`, docker compose |
| `dialpad/webhook.py` | WebhookVerifier | Verifies the signed JWT body of incoming webhooks. Per-company secrets are derived from `WEBHOOK_SECRET_KEY`, so no database lookup is needed. | Webhook-signature middleware; `SubscriptionReconciler` (registers each company's secret) |
| `dialpad/id_token.py` | IdTokenVerifier | Verifies the iframe's per-user `id_token`. The format is assumed and unconfirmed: see open question #2. Dev tokens are minted by `stubs/dialpad_tokens.py`. | Auth middleware |

## Runtime

| Adapter | Port | What it does | Used by |
|---|---|---|---|
| `scheduler/memory.py` | Scheduler | asyncio timers with jitter | `bootstrap.startup()`, which runs the schedules features register: outbox relay (15 s), expired-record purge (`PURGE_INTERVAL_SECONDS`), weekly subscription sweep tick, change-log poll (5 min) |
| `changelog/noop.py` | ChangeLogSource | Placeholder for Dialpad's change log, which would trigger reconciles when users or departments change. Returns nothing; a scripted variant feeds events in tests. | `ChangeLogWatcher` (platform feature) |

## HTTP: the web-framework adapter

**`http/adapters/sanic.py`** is the only module that imports Sanic (a lint test enforces this):
- it turns Sanic requests into the framework-neutral `HttpRequest`, calls `HttpApp.handle`, and
  converts the result back;
- with `EMBEDDED_WORKERS=true`, it also starts the worker runtime and scheduler alongside the
  HTTP server.

It's used only by `framework-sanic serve`. The black-box HTTP suite (`tests/http_contract/`) runs
against both this adapter and the neutral dispatcher, so another web framework could be added as
one more adapter.

## Connectors (the external system)

Connectors implement the `Connector` port: auth, the UI manifest, contact lookup and creation,
and error mapping. Anything else (logging calls, SMS, notes) is the integration's own methods,
called by its own listeners. Unlike the adapters above, connectors are plugins: each lives in
its own integration package, is built on the SDK's `RestConnector`, and is found through the
`framework_sanic.connectors` entry point (`CONNECTOR=<name>`).

| Connector | What it is | Used for |
|---|---|---|
| `integrations/hubspot` | HubSpot CRM v3 objects + v4 associations. Each record stores the Dialpad activity key so retries never duplicate. | Production. Tested against an API mock only; no sandbox verification yet. |
| `stubs/connector` (stub) | In-memory CRM (contacts only; `fake.calls`/`fake.sms`/`fake.notes` record deliveries), plus the `fake.dev_harness` feature for inspecting and seeding it. Selected with `CONNECTOR=fake`. | Tests, `scripts/dev.sh`, docker compose |

**Used by:**
- **OAuth routes and `ExternalCredentialService`:** CRM authorization and token refresh.
- **`ContactService`:** contact card lookup, search, rematch, and creating contacts. Matching is the connector's `match_contact`; the result is stored as an `EntityMap` and reused until a rematch replaces it.
- **`/session`:** the UI manifest.
- **`SyncGuards`:** connector name for rate buckets; mapping errors to retry or deadletter.
- **The integration's listeners:** its own methods (HubSpot: `upsert_call`, `attach_recording`,
  `add_note`, `log_sms`, `find_by_reference`).

## Which adapters run where

| Environment | Storage | Queue | Cache | KMS | Dialpad | Connector |
|---|---|---|---|---|---|---|
| Tests (`tests/conftest.py`) | memory (SQLAlchemy and Datastore in the contract suite) | memory | memory (Redis via fakeredis in the contract suite) | local | stub | stub (HubSpot against a mock) |
| `scripts/dev.sh` | SQLAlchemy + SQLite | memory | Redis | local | stub | stub |
| docker compose | Datastore emulator | memory (Pub/Sub emulator with `QUEUE_ADAPTER=pubsub`) | Redis | local | stub | stub |
| Production (intended) | Datastore | Pub/Sub | Redis | GCP KMS | real Dialpad | HubSpot |

**Exercised so far:**
- the memory adapters;
- SQLAlchemy on SQLite;
- Redis, against fakeredis and once against a real Redis 7 server;
- local KMS, the Dialpad stub and Sanic.

The Datastore, Pub/Sub, GCP KMS and real Dialpad adapters are written but have never run, so the
production row is untested end to end.
