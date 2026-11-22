# Integration Framework (Dialpad ⇄ external systems)

A reusable framework that embeds in Dialpad and connects it to external systems (CRMs,
ticketing). **The framework receives Dialpad events** (auth, webhook subscriptions, verified
buffer-first intake, durable fair delivery) **and hands them to integration features**; what an
event means for a given integration is the feature's business logic. It also serves an embedded
Vue 3 UI that shows the matched external contact and lets the user rematch it.

Two independent opt-in reference features ship with it: `call_logging` logs calls as activities
in the external service (one activity per call leg) and adds call notes; `sms_logging` logs SMS.
Enable either or both (`FEATURES=call_logging,sms_logging`) and tune them with settings
(`CALL_LOGGING_*`, `SMS_LOGGING_*`).

**Integrations are packages.** Each one (`integrations/<name>/`, a uv workspace member) has its
own `pyproject.toml`, registers its connector and features through entry points, imports only the
public SDK (`framework_sanic.sdk`) and tests itself with the framework's test kit
(`framework_sanic.testing`). The framework stays a repository for now, but nothing in an
integration depends on that: when the framework is published as a package, an integration moves
to its own repository unchanged. HubSpot (`integrations/hubspot`) is the reference integration;
a fake CRM drives tests and local development.

- Design rules, data model and build order: `integration-framework-agent-brief.md`
- System overview (high-level walkthrough): [`docs/system-overview.md`](docs/system-overview.md)
- What each adapter does and where it is used: [`docs/adapters.md`](docs/adapters.md)
- Unconfirmed assumptions: [`docs/open-questions.md`](docs/open-questions.md)

## Architecture

```
src/framework_sanic/
  sdk/                  THE public API for integrations (semver once published); everything else is internal
  core/                 plain Python: no web framework, no infrastructure clients
    domain/             entities (deterministic keys, schema_version), events, errors
    ports/              Repository/UnitOfWork, Queue, Cache, ParentPlatform, Connector, TokenStore,
                        KeyProvider, IdTokenVerifier, WebhookVerifier, Scheduler, ChangeLogSource
    intake/             accept path (verify -> encrypt -> fan out per handler) and delivery to handlers
    services/           subscription_reconciler, target_hierarchy, credential_service, contact_service,
                        company_service, outbox, rate_limit, crypto, phone
    features/           FeatureContext + registry (entry-point discovery), job results, SyncGuards;
                        platform (always on: reconcile, sweep, change log, outbox relay, purge)
  features/             shared opt-in features: call_logging (fold, sync, notes, policy), sms_logging
  connectors/rest.py    RestConnector + OAuth2Auth: the toolkit HTTP/JSON connectors are built on
  adapters/             dialpad (REST client, webhook + id_token verifiers), storage (memory,
                        sqlalchemy, datastore), queue (memory, pubsub), cache (memory, redis),
                        kms (local, gcp), scheduler, changelog (no-op)
  http/                 framework-neutral routes, middleware pipeline, handlers
    adapters/sanic.py   the only module that imports sanic (also serves the packaged UI at /app/)
  stubs/                fakes for tests + local dev, never imported by production code (lint-enforced):
                        dialpad (FakeDialpadPlatform), dialpad_tokens (dev id_tokens),
                        connector (FakeConnector + fake.dev_harness feature)
  testing/              test kit (`testing` extra): harness + pytest plugin, connector and repository
                        contract suites, cassette record/replay
  tools/                CLI tools: new (scaffold), dev, replay (fake Dialpad driver), check, schemas
  workers/              runtime (fair pools), per-topic worker entry points
  bootstrap.py          composition root: config -> adapters -> services -> plugins (entry points)
  ui_dist/              the built UI (`pnpm -C ui build`; git-ignored, shipped in the wheel)
integrations/           one package per integration (uv workspace members): hubspot/
ui/                     pnpm workspace: ui-shell, ui-kit, api-client (generated), connector-ui-hubspot
tests/                  framework tests: unit, contract, property, http_contract, integration
docker/                 integration.Dockerfile (integration image FROM the framework base image)
deploy/                 terraform module (Cloud Run api + workers) and a CI/CD template
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

## Building an integration

An integration is a connector (how to talk to the external system) plus, when the built-in
features do not fit, its own features (what to do with Dialpad events). The workflow:

```bash
uv run framework-sanic new zendesk --feature tickets   # integrations/zendesk, joined to the workspace
#   implement the TODOs in integrations/zendesk/src/framework_sanic_zendesk/connector.py
uv run framework-sanic check integrations/zendesk      # the gate; failing contract tests are the to-do list
uv run framework-sanic dev --connector zendesk         # run it locally (fake Dialpad, your connector)
```

`new` generates a package with entry points, a `RestConnector` skeleton implementing the
`call_logging` and `sms_logging` ports, an in-memory fake of the external API, the connector
contract suite wired to it, and (with `--feature`) feature skeletons with a harness test.

### Connectors

Build on `RestConnector` (`framework_sanic.sdk`): it brings `OAuth2Auth` (authorization code +
PKCE, refresh keeping non-rotated refresh tokens, an `identify` hook for account metadata),
bearer-authenticated `get/post/put/patch/delete`, one quick retry of idempotent requests,
`paginate` and `map_error` (401 → `auth`, 429 → `rate_limited` honouring `Retry-After`, 404 →
`not_found`, 408/5xx/network → `transient`, other 4xx → `permanent`). The connector then needs:

1. `ui_manifest()` (drives the contact card; no frontend code), contact `search_entity` (by
   phone variants, email or text), `get_entity`, `create_entity`, and **`match_contact`**: the
   integration's rule for which contact a Dialpad caller is (`Caller` → `ContactMatch`), used by
   the contact card and when calls/SMS are logged. `match_by_phone` from the SDK is a simple
   starting point.
2. The ports of the features it supports; a feature refuses to start with a connector lacking it:
   - `call_logging` → `CallLogger` (`framework_sanic.sdk.call_logging`): `upsert_call`,
     `attach_recording`, `add_note`, `find_by_reference`.
   - `sms_logging` → `SmsLogger` (`framework_sanic.sdk.sms_logging`): `log_sms`, `find_by_reference`.

   Idempotency: store the activity key (`activity.activity_key`) on the external record and make
   `find_by_reference` find it; the features always call it before creating.

   **`EntityMap`** is how Dialpad objects and external records are tied together, and the only
   reference type connectors see: `dialpad_id` → `external_type` + `external_id` (types are the
   integration's own, e.g. `contact`, `lead`, `calls`). The framework stores one per company and
   Dialpad id:
   - **contacts** (`dialpad_id` = `contact:<id>` or `phone:<number>`): the first match from
     `match_contact` is stored and reused; a rematch replaces it (both are treated the same). A
     stored contact the CRM no longer has (`not_found`) is dropped and matched again.
   - **logged records** (`dialpad_id` = the activity key): what `upsert_call`, `log_sms`,
     `add_note` and `find_by_reference` return. `upsert_call` receives the stored map as
     `existing` to update instead of create; `attach_recording`/`add_note` receive the call's.

   Activities keep a copy of the contact map they were logged with (`activity.contact`), so a
   rematch only affects future activities. Search and match results (`Entity`) carry
   `external_id` and `external_type` directly.
3. Its own settings: a `PluginSettings` subclass with an env prefix (`HUBSPOT_*`), loaded with
   `load_settings` in the entry-point factory `make(settings) -> Connector`.
4. Optional hooks for the built-in features: `should_log_call(activity)` (`CallLogFilter`) and
   `should_log_sms(activity)` (`SmsLogFilter`) for rules the settings cannot express.

Register it in the package's `pyproject.toml` (`[project.entry-points."framework_sanic.connectors"]
name = "pkg:make"`) and select it with `CONNECTOR=name`.

### Features

Features are where integration business logic lives; the framework only delivers events to them.
Expose `feature = MyFeature()` with a `name` and `setup(ctx: FeatureContext)`, register it under
`[project.entry-points."framework_sanic.features"]` (`"<integration>.<feature>" = "pkg.module:feature"`)
and enable it with `FEATURES=`. In `setup`:

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
  `ctx.declare_company_data(*entities)` (deleted on uninstall), `ctx.settings(MySettings)`.

Use shared services via `ctx.services` (Repository, credentials, connector, queue, cache) and the
SDK toolkit: `publish_and_clear` / `run_in_uow` (transactional follow-up jobs) and `SyncGuards`
(parking, credentials, rate budgets, leases, error mapping) for jobs calling the connector.
Features cannot import each other, and core never imports features (both lint-enforced).

### Testing

The test kit (`framework_sanic.testing`) gives every integration the same tools the framework uses:

- `ConnectorContract`: subclass it with your connector and a fake (or recorded) backend.
- The `harness` fixture (auto-loaded pytest plugin): the whole framework in-process with memory
  adapters and fake Dialpad; override `harness_connector` / `harness_settings` to run it with
  your connector. `harness.webhook(...)`, `harness.drain()`, `call_event(...)`, `sms_event(...)`.
- `CassetteTransport`: record real API responses once against a sandbox
  (`CASSETTE_MODE=record`), replay them in CI.

### The gate: `framework-sanic check`

Run on every integration (CI does), it checks the **SDK boundary** (integration code imports only
`framework_sanic.sdk`, tests also `framework_sanic.testing`), that **entry points** are installed
and load, that the **wiring** starts (each feature, and the logging features each connector
supports, with scopes and ports validated), that a **contract suite** exists, and that the
**tests** pass. It also works outside this repository, against an installed framework.

### Moving to a published package later

Nothing in `integrations/*` refers to this repository: drop the
`[tool.uv.sources] framework-sanic = { workspace = true }` line, depend on a released
`framework-sanic`, and the package works from its own repository. Its image is
`docker/integration.Dockerfile` with `INTEGRATION=.` on top of the framework base image.

## Setup

### What gets installed

The package always contains core, the SDK, every adapter, the stubs and the test kit, but only
the memory adapters work without extras: each backend's client library comes with one. Selecting
an adapter whose extra is missing fails at startup with the `pip install` line to run.

| Install | Adds | For |
|---|---|---|
| `framework-sanic` | core, SDK, memory adapters, HTTP | integration code |
| `[testing]` | pytest, pytest-asyncio | integration tests (harness, contract suites) |
| `[dev-stack]` = `[redis,sql]` | Redis client, SQLAlchemy + aiosqlite | `framework-sanic dev` (SQLite + Redis) |
| `[gcp]` | Datastore, Pub/Sub, Cloud KMS clients | production (`STORAGE_ADAPTER=datastore`, `QUEUE_ADAPTER=pubsub`, `KMS_KEY`) |
| `[redis]`, `[sql]` | one backend each | `CACHE_URL=redis://...`, `STORAGE_ADAPTER=sqlalchemy` |

In this repository `uv sync --all-extras` installs everything; the base image installs
`[gcp,redis]`. An integration package depends on plain `framework-sanic`.

### One command: server + UI + SQLite + Redis

```bash
uv sync --all-extras
uv run framework-sanic dev                      # API on :8000, UI, SQLite at .local/dev.db, Redis on :6379
uv run framework-sanic dev --connector hubspot  # any installed connector (--features a,b for features)
uv run framework-sanic dev --ui vite            # Vite dev server with hot reload on :5173
uv run framework-sanic dev --cache memory       # no Redis
uv run framework-sanic dev --reset --no-seed    # wipe the local database (and flush Redis), start empty
scripts/dev.sh [same flags]                     # wrapper: uv sync + dev (`--server-only` = `--ui none`)
```

It runs the service with the fake Dialpad platform (`src/framework_sanic/stubs/`), SQLite
(`STORAGE_ADAPTER=sqlalchemy`), **Redis as the cache**, the in-process queue and embedded
workers. For Redis it reuses one already listening on `--redis-port` (6379), otherwise starts
`redis-server` (`apt install redis-server` / `brew install redis`), otherwise a `redis:7-alpine`
Docker container, and stops whatever it started on exit; `CACHE_URL=...` points it elsewhere.
The UI is the prebuilt one shipped in the package (served at `/app/`) when present, else the
Vite dev server when pnpm is available. With the fake CRM it seeds demo data (admin connect, CRM
connect, a demo contact, one logged call) and prints a UI URL carrying a dev `id_token`. Logs go
to `.local/logs/`; Ctrl-C stops everything. Any setting can be overridden through env vars.

### Manual

```bash
uv sync --all-extras                        # Python 3.12+, installs dev tools and integrations/*
cp .env.example .env                        # local config (fake Dialpad + fake connector, Redis cache)
redis-server --save "" &                    # or set CACHE_URL=memory:// in .env
uv run framework-sanic serve                # http://localhost:8000 (workers embedded)
uv run framework-sanic replay e2e           # connect, replay a call, verify it reached the connector
uv run framework-sanic replay sms           # send one SMS event
```

Run `serve` with `FEATURES=fake.dev_harness` (as in `.env.example`) for the `e2e` command.

### Checks (the build gates)

```bash
uv run pytest                              # all suites (framework tests/ and integrations/*)
uv run pytest tests/unit/test_import_rules.py   # import-lint: sanic only in http/adapters/sanic.py
uv run mypy                                # src + integrations/*/src
uv run mypy src/framework_sanic/core --strict
uv run ruff check src tests integrations
uv run framework-sanic check               # every integration: SDK boundary, plugins, contract, tests
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
| 8 connector + call_logging stage 2 | `framework_sanic.testing.connector_contract` on the fake CRM (`tests/contract/test_connector_contract.py`) and every integration, `tests/integration/test_stage2.py`, `tests/integration/test_logging_options.py` |
| 9 fairness | `tests/integration/test_fairness.py` |
| 10 UI | `ui/` → `pnpm install && pnpm test && pnpm e2e` |
| 11 HubSpot | `integrations/hubspot/tests` (contract against a HubSpot API mock + end to end through the harness); sandbox verification still to do |
| 12 SDK and tooling | `tests/unit/test_sdk.py` (SDK exports, boundary, scaffolding, check), `tests/unit/test_rest_connector.py`, `tests/unit/test_optional_dependencies.py` (plain install, missing-extra errors) |

### Local harness (docker compose)

```bash
docker compose up --build                       # Datastore emulator, Redis, Pub/Sub emulator, app, fake-dialpad
docker compose run --rm contract-tests          # contract suites against the emulators + Redis
QUEUE_ADAPTER=pubsub docker compose up --build  # use the Pub/Sub emulator as the queue
```

The `fake-dialpad` service performs admin OAuth and external OAuth, replays a shuffled and
duplicated call sequence as signed webhooks, and exits 0 once exactly one external record with
the recording exists in the fake connector. Replay your own sequences with
`framework-sanic replay replay-file events.json --shuffle --duplicates 3`.

### Images and deployment

```bash
docker build --target base -t framework-sanic-base .            # framework runtime + prebuilt UI
docker build -f docker/integration.Dockerfile --build-arg BASE_IMAGE=framework-sanic-base \
    --build-arg INTEGRATION=integrations/hubspot -t integration-hubspot .
```

The `base` image is non-root, has no dev tools and contains the built UI; an integration image
adds one integration package on top. `deploy/terraform/modules/integration` runs it on Cloud Run
(an API service and an always-on worker service on Datastore, Pub/Sub, Memorystore and KMS;
`examples/hubspot` shows the inputs) and `deploy/ci/github-actions.yml` is the pipeline template
(checks, UI build, images, Datastore indexes, deploy). Both are templates and not yet validated
(`terraform validate`, a real deploy).

### UI

```bash
cd ui && pnpm install  # first install creates pnpm-lock.yaml: commit it (CI and Docker use it)
pnpm generate:api      # regenerate api-client types from the Pydantic models
pnpm dev               # Vite on :5173, proxies the API to :8000
pnpm test              # component tests (vitest)
pnpm build             # production build into src/framework_sanic/ui_dist (served at /app/)
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
| `CONNECTOR` | `fake` | an installed connector (`framework_sanic.connectors` entry point: `fake`, `hubspot`, ...) or `package.module:factory` |
| `FEATURES` | empty | features to enable (`framework_sanic.features` entry points or module paths), e.g. `call_logging,sms_logging,hubspot.sms_campaign`. Without a feature that handles events nothing is subscribed or processed (a warning is logged). `platform` is always on |
| `CALL_LOGGING_DIRECTIONS`, `CALL_LOGGING_SKIP_STATES` | `inbound,outbound`, empty | which calls `call_logging` logs (e.g. skip `missed,voicemail`) |
| `CALL_LOGGING_MIN_DURATION_SECONDS`, `CALL_LOGGING_RECORDINGS` | `0`, `true` | skip short completed calls; attach recording links |
| `SMS_LOGGING_DIRECTIONS`, `SMS_LOGGING_INCLUDE_TEXT` | `inbound,outbound`, `true` | which SMS `sms_logging` logs; `false` logs them without the text (never decrypted) |
| connector settings | | owned by each integration with its own prefix, e.g. `HUBSPOT_*` (see `integrations/hubspot/README.md`) |
| `STORAGE_ADAPTER` | `memory` | `memory`, `sqlalchemy` (`DATABASE_URL`; `[sql]` extra), `datastore` (`DATASTORE_PROJECT`; `[gcp]`), or `package.module:factory` for your own adapter (see below). Anything else fails at startup |
| `QUEUE_ADAPTER` | `memory` | `memory` or `pubsub` (`PUBSUB_PROJECT`, `PUBSUB_PREFIX`; `[gcp]`) |
| `CACHE_URL` | `memory://` | `redis://…` (`[redis]`) for local dev and real deployments; tests use `memory://` |
| `KMS_KEY` | empty | GCP KMS key name (`[gcp]`); empty = local key (`LOCAL_MASTER_KEY`) |
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
| `SERVE_UI_DIR` | unset | directory of a built UI to serve at `/app/`; default: the build shipped in the package, when present (VERIFY hosting) |

## Writing a storage adapter (framework team)

Integrations do not choose infrastructure: the framework owns the adapters and the deployment
sets them. This section is for changing or adding a backend.

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

## Privacy and security

- SMS text is never stored: it is encrypted (envelope encryption, KMS-wrapped DEK) inside the
  queue message and dropped after sync. Call transcripts are dropped at parse time.
- Tokens are encrypted at rest through the `KeyProvider`; Redis never holds refresh tokens.
- Call and SMS activity records expire after `ACTIVITY_TTL_DAYS`; `POST /admin/uninstall`
  deletes all company data, including every kind enabled features declared; connects, disconnects, config changes and deletions go to the audit log (kept).
- CSP `frame-ancestors` restricts embedding to Dialpad origins.
