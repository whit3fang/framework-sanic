# Integration Framework (Dialpad ⇄ external systems)

A reusable framework that embeds in Dialpad and connects it to external systems (CRMs,
ticketing). **The framework receives Dialpad events** (auth, webhook subscriptions, verified
buffer-first intake, durable fair delivery) **and hands them to integration features**; what an
event means for a given integration is the feature's business logic. It also serves an embedded
Vue 3 UI that shows the matched external contact and lets the user rematch it.

Two independent opt-in reference features ship with it: `call_logging` logs calls as activities
in the external service (one activity per call leg) and adds call notes; `sms_logging` logs SMS.
Enable either or both (`FEATURES=call_logging,sms_logging`). HubSpot is the first connector; a
fake connector drives tests and the local harness.

- Design rules, data model and build order: `integration-framework-agent-brief.md`
- System overview (high-level walkthrough): [`docs/system-overview.md`](docs/system-overview.md)
- What each adapter does and where it is used: [`docs/adapters.md`](docs/adapters.md)
- Unconfirmed assumptions: [`docs/open-questions.md`](docs/open-questions.md)

## Architecture

```
src/framework_sanic/
  core/                 plain Python: no web framework, no infrastructure clients
    domain/             entities (deterministic keys, schema_version), events, errors
    ports/              Repository/UnitOfWork, Queue, Cache, ParentPlatform, Connector, TokenStore,
                        KeyProvider, IdTokenVerifier, WebhookVerifier, Scheduler, ChangeLogSource
    intake/             accept path (verify -> encrypt -> fan out per handler) and delivery to handlers
    services/           subscription_reconciler, target_hierarchy, credential_service, contact_service,
                        company_service, outbox, rate_limit, crypto, phone
    features/           FeatureContext + registry, job results, SyncGuards; platform (always on:
                        reconcile, sweep, change log, outbox relay)
  features/             shared opt-in features: call_logging (fold, sync, notes), sms_logging
  adapters/             dialpad (REST client, webhook + id_token verifiers), storage (memory,
                        sqlalchemy, datastore), queue (memory, pubsub), cache (memory, redis),
                        kms (local, gcp), scheduler, changelog (no-op)
  http/                 framework-neutral routes, middleware pipeline, handlers
    adapters/sanic.py   the only module that imports sanic
  connectors/           hubspot/ (+ features/sms_campaign placeholder)
  stubs/                fakes for tests + local dev, never imported by production code (lint-enforced):
                        dialpad (FakeDialpadPlatform), dialpad_tokens (dev id_tokens),
                        connector (FakeConnector + fake.dev_harness feature)
  testing/              reusable contract suites for adapter authors (`testing` extra)
  workers/              runtime (fair pools), per-topic worker entry points
  bootstrap.py          composition root: config -> adapters -> services -> features
ui/                     pnpm workspace: ui-shell, ui-kit, api-client (generated), connector-ui-hubspot
tests/                  unit, contract, property, http_contract, integration (gates 4-9)
tools/fake_dialpad.py   fake Dialpad driver: connect + signed webhook replay (shuffled/duplicated)
```

The package lives under `src/framework_sanic/` (rather than top-level `src/core`, `src/http`, …)
so that `http` does not shadow the standard library and the project installs as one package.

**Framework: receive and deliver.** `POST /webhooks/dialpad/{company_id}/events` → verify HS256
JWT (derived per-company secret, no DB) → parse → encrypt SMS text → publish **one message per
registered handler** to the durable queue (sharded by company hash) → `2xx`. Works with Redis and
the database down; never returns 4xx/429 for a deliverable event (signed-but-unparseable events
are dead-lettered with 200; queue failures return 503 so Dialpad retries). Workers deliver each
message to its feature's handler (`ctx.on_event`) at least once, with lanes, per-company
fairness, and retry/backoff/deadletter driven by the handler's `JobResult`. Dialpad subscriptions
are created only for event kinds some enabled feature handles. Toolkit for handlers: atomic
repository upserts, the transactional outbox (+ relay), `SyncGuards` (parking, credentials,
rate budgets, leases, connector error mapping).

**`call_logging` (opt-in business logic).** Stage 1 folds call events into `CallActivity`
(order-independent max-rank fold; terminal states never regress; recordings merge additively)
and writes an outbox entry in the same UnitOfWork. Stage 2 syncs to the connector through the
`CallLogger` port with `find_by_reference` idempotency, then attaches recordings and notes.

**`sms_logging` (opt-in business logic).** Inserts `SmsActivity` once (skipped if the first
status seen is failed), then a sync job decrypts the text transiently and logs it through the
`SmsLogger` port. The text is never stored.

**UI path.** iframe → Vue app → HTTP layer (`id_token` → in-memory session token) → connector live
reads with the acting user's credential.

## Setup

### One command: server + UI + SQLite + Redis

```bash
scripts/dev.sh                 # API on :8000, Vite UI on :5173, SQLite at .local/dev.db, Redis on :6379
scripts/dev.sh --server-only   # no Node/pnpm needed
scripts/dev.sh --reset         # wipe the local database (and flush a reused Redis) first
scripts/dev.sh --no-seed       # start empty
```

The script runs the service with the fake Dialpad platform and fake connector (both from
`src/framework_sanic/stubs/`), SQLite (`STORAGE_ADAPTER=sqlalchemy`), **Redis as the cache**, the
in-process queue and embedded workers. For Redis it reuses one already listening on
`REDIS_PORT` (6379), otherwise starts `redis-server` (`apt install redis-server` /
`brew install redis`), otherwise a `redis:7-alpine` Docker container, and stops whatever it
started on exit. `CACHE_URL=...` points it at another Redis; `CACHE_URL=memory://` opts out. It then
seeds demo data (admin connect, CRM connect, a demo contact, one logged call) and prints a UI URL
that carries a dev `id_token` and the call context (`dev_call_id`/`dev_phone`, honoured only in
Vite dev builds). Logs go to `.local/logs/`; Ctrl-C stops both processes. Ports and any setting
can be overridden through env vars, e.g. `API_PORT=9000 UI_PORT=5174 scripts/dev.sh`.

### Manual

```bash
uv sync --all-extras                       # Python 3.12+, installs dev tools
cp .env.example .env                       # local config (fake Dialpad + fake connector, Redis cache)
redis-server --save "" &                   # or set CACHE_URL=memory:// in .env
uv run framework-sanic serve               # http://localhost:8000 (workers embedded)
uv run python tools/fake_dialpad.py e2e    # connect, replay a call, verify it reached the connector
```

Run `serve` with `FEATURES=fake.dev_harness` (as in `.env.example`) for the `e2e` command.

### Checks (the build gates)

```bash
uv run pytest                              # all suites
uv run pytest tests/unit/test_import_rules.py   # import-lint: sanic only in http/adapters/sanic.py
uv run mypy src/framework_sanic/core       # strict on core (whole package also passes)
uv run ruff check src tests
```

| Gate | Suite |
|---|---|
| 1 skeleton, ports, memory adapters, import lint | `tests/unit/test_import_rules.py`, `tests/contract/test_repository_contract.py[memory]` |
| 2 Datastore (+SQLAlchemy) | `tests/contract/test_repository_contract.py` (the suite lives in `framework_sanic.testing.repository_contract`; Datastore needs `DATASTORE_EMULATOR_HOST`) |
| 3 HTTP layer | `tests/http_contract/` (runs against every HTTP adapter) |
| 4 Dialpad auth | `tests/integration/test_dialpad_auth.py` |
| 5 accept path + delivery | `tests/integration/test_accept_path.py`, `tests/integration/test_event_delivery.py` (framework/feature boundary) |
| 6 call_logging stage 1 merge | `tests/property/test_call_fold.py`, `tests/integration/test_stage1.py` |
| 7 reconciler | `tests/integration/test_reconciler.py` |
| 8 connector + call_logging stage 2 | `tests/contract/test_connector_contract.py`, `tests/integration/test_stage2.py` |
| 9 fairness | `tests/integration/test_fairness.py` |
| 10 UI | `ui/` → `pnpm install && pnpm test && pnpm e2e` |
| 11 HubSpot | connector contract against a HubSpot API mock; sandbox verification still to do |

### Local harness (docker compose)

```bash
docker compose up --build                       # Datastore emulator, Redis, Pub/Sub emulator, app, fake-dialpad
docker compose run --rm contract-tests          # contract suites against the emulators + Redis
QUEUE_ADAPTER=pubsub docker compose up --build  # use the Pub/Sub emulator as the queue
```

The `fake-dialpad` service performs admin OAuth and external OAuth, replays a shuffled and
duplicated call sequence as signed webhooks, and exits 0 once exactly one external record with
the recording exists in the fake connector. Replay your own sequences with
`python tools/fake_dialpad.py replay-file events.json --shuffle --duplicates 3`.

### UI

```bash
cd ui && pnpm install
pnpm generate:api      # regenerate api-client types from the Pydantic models
pnpm dev               # Vite on :5173, proxies the API to :8000
pnpm test              # component tests (vitest)
```

The session token is kept in memory only. All external data is rendered with text interpolation
(no `v-html`); links from external data are restricted to http(s). `postMessage` origins are
validated against `VITE_DIALPAD_ORIGINS`.

## Configuration

All settings come from env / `.env` locally and terraform in beta/production
(`src/framework_sanic/core/config.py`). Lists are comma separated.

| Key | Default | Purpose |
|---|---|---|
| `DIALPAD_CLIENT_ID`, `DIALPAD_CLIENT_SECRET` | dev values | Dialpad OAuth app |
| `DIALPAD_SCOPES` | `recordings_export,message_content_export,calls:list,change_log` | startup fails if an enabled feature needs a scope not listed |
| `DIALPAD_IFRAME_URL` | `http://localhost:8000/app/` | URL registered for the iframe |
| `DIALPAD_CALL_STATES` | `hangup,missed,voicemail,recording` | call subscription states (VERIFY) |
| `LOGGABLE_TARGET_TYPES` | `user,department,callcenter` | target types that get subscriptions |
| `CONNECTOR` | `fake` | `fake` or `hubspot` |
| `FEATURES` | empty | features to enable, e.g. `call_logging,sms_logging,hubspot.sms_campaign`. Without a feature that handles events nothing is subscribed or processed (a warning is logged). `platform` is always on |
| `STORAGE_ADAPTER` | `memory` | `memory`, `sqlalchemy` (`DATABASE_URL`), `datastore` (`DATASTORE_PROJECT`), or `package.module:factory` for your own adapter (see below). Anything else fails at startup |
| `QUEUE_ADAPTER` | `memory` | `memory` or `pubsub` (`PUBSUB_PROJECT`, `PUBSUB_PREFIX`) |
| `CACHE_URL` | `memory://` | `redis://…` for local dev (`.env.example`, `scripts/dev.sh`) and real deployments; tests use `memory://` |
| `KMS_KEY` | empty | GCP KMS key name; empty = local key (`LOCAL_MASTER_KEY`) |
| `SHARD_COUNT` | `8` | queue shards (company hash) |
| `ACTIVITY_TTL_DAYS` | `90` | TTL of call/SMS activity records |
| `PURGE_INTERVAL_SECONDS`, `PURGE_BATCH_SIZE`, `PURGE_MAX_BATCHES` | `3600`, `1000`, `50` | how often and how much the platform feature physically deletes expired records (they are hidden on read immediately) |
| `CHANGELOG_LIFECYCLE_ACTIONS` | `target_created,target_deleted` | change-log actions that trigger a reconcile |
| `PLATFORM_ADAPTER` | `dialpad` | `fake` for the local harness |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | used for OAuth redirects and webhook URLs |
| `WEBHOOK_SECRET_KEY` | dev value | master key for derived per-company webhook secrets |
| `SESSION_SECRET`, `SESSION_TTL_SECONDS` | dev / `900` | UI session tokens |
| `DIALPAD_RATE_LIMIT_PER_MINUTE` | `1200` | per-company Dialpad API budget (shared by reconciler and UI) |
| `CONNECTOR_RATE_LIMIT_PER_MINUTE` | `600` | per-company connector budget |
| `PER_COMPANY_CONCURRENCY`, `WORKER_CONCURRENCY` | `4`, `32` | fairness caps per worker pool |
| `UNKNOWN_CALLER_POLICY` | `log_unassociated` | VERIFY |
| `EMBEDDED_WORKERS` | `true` | run workers inside the HTTP process (local); run `framework-sanic worker` separately in prod |
| `SERVE_UI_DIR` | unset | serve the built UI at `/app/` (VERIFY hosting) |

## Writing a storage adapter

Storage sits behind the `Repository` port (`core/ports/repository.py`); its docstring is the
contract. Built in: `memory` (tests), `sqlalchemy` (SQLite/PostgreSQL) and `datastore`.

1. Implement `Repository` and `UnitOfWork`. The framework needs, from any backend:
   - `upsert(kind, key, merge_fn)`: an atomic read-modify-write on one record, returning
     `created` / `updated` / `unchanged` / `ignored`; records must carry `schema_version`.
   - `unit_of_work()`: a small multi-record transaction (an activity change + its outbox entry)
     that commits atomically, raising `Conflict` on concurrent changes (`run_in_uow` retries).
   - `get`, `delete` and `query(kind, **equality_filters)` over top-level fields.
   - Expiry: a record whose `expires_at` (epoch seconds) has passed reads as absent.
   - `setup(schema)`: idempotent, called once at startup with every stored kind and the indexes
     features declared (`ctx.declare_index`). Create or verify tables and indexes here.
   - `purge_expired(limit=...)`: physically delete up to `limit` expired records and return the
     count. The platform feature runs it on a schedule; backends with native TTL may use both.
   - `ping()` and `close()`.
2. Expose a factory `make(settings, clock) -> Repository` and select it with
   `STORAGE_ADAPTER=yourpackage.module:make`. Use the injected `clock` for expiry (tests control it).
3. Run the contract suite against it (`pip install framework-sanic[testing]`):

   ```python
   import pytest_asyncio
   from framework_sanic.testing.repository_contract import ContractClock, RepositoryContract

   class TestMyAdapter(RepositoryContract):
       @pytest_asyncio.fixture
       async def repository(self, clock: ContractClock):
           repo = MyRepository(DSN, clock=clock)  # fresh and isolated per test
           yield repo
           await repo.close()
   ```

   It covers atomicity under concurrency, unit-of-work rollback and conflicts, expiry, purge,
   query filters and pagination. Passing it proves correctness, not performance: load test the
   adapter at your traffic before relying on it.
4. Datastore specifics: indexes and TTL are deployed out of band. `framework-sanic
   export-indexes` writes `index.yaml` for the enabled `FEATURES` and prints the optional native
   TTL commands (`purge_expired` deletes expired records either way).

## Adding a connector

1. Create `src/framework_sanic/connectors/<name>/connector.py` implementing the `Connector`
   protocol (`core/ports/connector.py`): `auth` (a `ConnectorAuth`: authorize URL, code exchange,
   refresh), `capabilities`, `ui_manifest()`, entity search/get/create and `map_error`.
2. Implement the ports of the features it should support (each feature refuses to start with a
   connector that lacks its port):
   - `call_logging` → `CallLogger` (`features/call_logging/port.py`): `upsert_call`,
     `attach_recording`, `add_note`, `find_by_reference`.
   - `sms_logging` → `SmsLogger` (`features/sms_logging/port.py`): `log_sms`, `find_by_reference`.

   Idempotency: store the activity key (`activity.activity_key`) on the external record and make
   `find_by_reference` find it; the features always call it before creating.
3. `map_error` must classify failures as `auth`, `rate_limited` (with `retry_after`),
   `transient`, `not_found` or `permanent`; `SyncGuards.on_error` decides retries and parking.
4. Register it in `bootstrap.make_connector` and add it to the `connector` fixture in
   `tests/contract/test_connector_contract.py` (with an HTTP mock of the external API).
5. Optional UI extras go in `ui/packages/connector-ui-<name>`; the contact card is driven by
   `ui_manifest()` and needs no connector code.

## Adding a feature

Features are where integration business logic lives; the framework only delivers events to them.

1. Create a package: shared features under `features/<name>/`, connector-specific features under
   `connectors/<connector>/features/<name>/`. Features cannot import each other, and core never
   imports features (both lint-enforced).
2. Expose `feature = MyFeature()` with a `name` and `setup(ctx: FeatureContext)`. In `setup`:
   - `ctx.on_event("call" | "sms", handler)` to receive Dialpad events:
     `async def handler(event: InboundEvent) -> JobResult`. `event.call` / `event.sms` are parsed
     models; `await event.sms_text()` decrypts SMS text (use it transiently, never store it).
     Delivery is at least once, so handlers must be idempotent. Each feature gets its own message
     per event, so its retries never re-run other features. Registering a handler also creates
     the Dialpad subscriptions for that kind.
   - `ctx.route(handler)` for handlers decorated with `@route(method, path, auth=...)`
     (`async def handler(req: Model, ctx: RequestContext) -> Model`),
   - `ctx.job(topic, name)(handler)` for follow-up queue jobs returning `ACK` / `retry(...)` /
     `deadletter(...)`, `ctx.schedule(name, interval, fn)`,
   - `ctx.require_scopes(...)`, `ctx.declare_config(...)`, `ctx.declare_index(kind, *fields)`,
     `ctx.declare_company_data(*entities)` (deleted on uninstall).
3. Use shared services via `ctx.services` (Repository, credentials, connector, queue, cache) and
   the toolkit: `services.outbox` (transactional follow-up jobs) and `features.guards.SyncGuards`
   (parking, credentials, rate budgets, leases, error mapping) for jobs calling the connector.
4. Enable it with `FEATURES=<name>`, `FEATURES=<connector>.<name>` or a module path.
   `tests/support/echo_feature.py` is a minimal example.

## Privacy and security

- SMS text is never stored: it is encrypted (envelope encryption, KMS-wrapped DEK) inside the
  queue message and dropped after sync. Call transcripts are dropped at parse time.
- Tokens are encrypted at rest through the `KeyProvider`; Redis never holds refresh tokens.
- Call and SMS activity records expire after `ACTIVITY_TTL_DAYS`; `POST /admin/uninstall`
  deletes all company data, including every kind enabled features declared; connects, disconnects, config changes and deletions go to the audit log (kept).
- CSP `frame-ancestors` restricts embedding to Dialpad origins.
