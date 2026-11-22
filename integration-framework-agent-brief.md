# Integration Framework: Coding Agent Brief

You are working on a reusable integration framework that embeds in Dialpad and connects it to
external systems (CRMs, ticketing). HubSpot is the first integration, built on the framework as
a separate package. The framework owns receiving and delivering Dialpad activity; each
integration decides what that activity becomes in its external system.

This brief describes the design as built. The README is the user guide and
`docs/system-overview.md` the walkthrough. Where something is marked **VERIFY**, it is an
unconfirmed assumption: keep it behind a port or config value, never hard-code it, and list it in
`docs/open-questions.md`.

The repository is not a published package yet, but it is laid out to become one: integrations
depend only on `framework_sanic.sdk` and are discovered through entry points.

## 1. What the system does

1. Authenticates with Dialpad: admin OAuth connect per company, and per-user `id_token` gating for
   the embedded UI.
2. Authenticates with the external service, using the user's own credential first and then the
   company's.
3. Subscribes to Dialpad call and SMS webhooks for each loggable target, only for event kinds an
   enabled listener handles.
4. Delivers every call and SMS event, at least once, to the integration's listeners. What they do
   with it (log an activity, update a ticket, nothing) is the integration's code. HubSpot logs one
   activity per call leg (`call_id`) and one per SMS.
5. Serves an embedded Vue 3 UI (in a Dialpad iframe) showing the matched external contact, with
   search, rematch, create contact and, when a note listener is enabled, notes.
6. Hosts integration-specific features (for example the HubSpot SMS campaign, out of scope).

The UI shows no call or SMS history.

## 2. Hard rules

- Core code is plain Python with no web framework imports. `sanic` may be imported only in
  `http/adapters/sanic.py`; a lint test fails the build otherwise.
- Everything external sits behind a port. Adapters are chosen by config.
- Integrations import only `framework_sanic.sdk` (and `framework_sanic.testing` in tests);
  `framework-sanic check` enforces it.
- Handlers are framework-neutral: `async def handler(req: Model, ctx: RequestContext) -> Model`,
  registered with `@route(method, path, auth=...)`.
- The webhook accept path is buffer-first: verify the signature, encrypt the SMS text, publish to
  the durable queue, return 2xx. It must work with Redis and the database down. Never return 4xx
  or 429 for a deliverable event (Dialpad does not retry 4xx); errors there return 503.
- Redis is never the source of truth and never holds refresh tokens. It holds OAuth state and
  PKCE, leases, caches, rate-limit buckets, the dedupe fast path and session revocations.
- Never store SMS text: it is encrypted inside the queue message and decrypted only to send it.
  Never store call transcripts.
- Never render external data as raw HTML (`v-html`) in the UI.
- Merge functions passed to `upsert` must be pure: adapters rerun them after a write conflict.
- No dev secrets in production: with the real Dialpad adapter the service refuses to start while
  any secret still has its public default.
- Python 3.12+, Pydantic v2, async everywhere, `mypy --strict` on `core/`.

## 3. Layout

```
src/framework_sanic/
  core/                     # no framework imports
    domain/                 # entities, events, records, errors
    ports/                  # ParentPlatform, Repository, UnitOfWork, TokenStore, KeyProvider,
                            # Queue, Cache, Connector, IdTokenVerifier, ChangeLogSource, Clock
    services/               # credentials, contacts (matching), subscription reconciler, target
                            # hierarchy, sessions, rate limits, leases, outbox, crypto, phone
    intake/                 # webhook accept path, InboundEvent, fan-out
    features/               # FeatureContext, registry, jobs, SyncGuards, platform feature
    config.py, container.py # Settings, Services
  listeners/                # CallListener, SmsListener, NoteListener + CallActivity,
                            # SmsActivity, NoteIn/NoteOut
  connectors/rest.py        # OAuth2Auth + RestConnector toolkit for HTTP/JSON connectors
  adapters/                 # dialpad/, storage/{memory,sqlalchemy,datastore}/, queue/{memory,
                            # pubsub}/, cache/{memory,redis}/, kms/, changelog/, scheduler/
  http/                     # routes.py, middleware.py, app.py, handlers/, adapters/sanic.py
  workers/                  # stage1 (events topic), stage2 (sync topic), reconciler, sweeper
  sdk/                      # the public API integrations import
  testing/                  # harness, repository/connector contract suites, pytest plugin
  stubs/                    # fake Dialpad, fake connector, fake listeners (tests and dev)
  tools/                    # CLI: new, dev, check, serve, worker, export-schemas, export-indexes
  bootstrap.py, cli.py
integrations/hubspot/       # framework_sanic_hubspot: connector + listeners (a uv workspace member)
ui/packages/                # ui-shell, ui-kit, api-client (generated), connector-ui-hubspot
deploy/                     # terraform/, ci/
tests/                      # unit, contract, property, integration, http_contract, load
```

Data flow:

- Events: Dialpad webhook → accept path → one queue message per enabled listener (topic
  `events`, sharded by company) → the listener's handler → follow-up jobs the listener queues
  (topic `sync`, usually through the outbox) → external service.
- UI: iframe → Vue app → HTTP layer → connector live reads with the acting user's credential.

## 4. Ports and contracts

### Repository (generic record store)

```python
class Repository(Protocol):
    async def get(self, kind: str, key: Key) -> Record | None: ...
    async def upsert(self, kind: str, key: Key, merge_fn: MergeFn) -> UpsertResult: ...
        # "created" | "updated" | "unchanged" | "ignored"; atomic read-modify-write
    async def delete(self, kind: str, key: Key) -> None: ...
    def query(self, kind: str, **equality_filters) -> AsyncIterator[Record]: ...
    async def find(self, kind: str, *, where: Sequence[Condition] = (),
                   order_by: Sequence[str] = (), limit: int = 100, offset: int = 0) -> list[Record]: ...
    async def count(self, kind: str, *, where: Sequence[Condition] = ()) -> int: ...
    def unit_of_work(self) -> UnitOfWork: ...   # upserts, deletes, outbox writes: all or none
    async def setup(self, schema: StorageSchema) -> None: ...   # idempotent; tables, indexes
    async def purge_expired(self, *, limit: int = 1000) -> int: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
```

- Every kind lives behind this port as a JSON record. An entity class defines `KIND` and a
  deterministic `key()`; adding a kind needs no table and no migration.
- Every record carries `schema_version`. A record whose `expires_at` has passed reads as absent.
- `find` / `count`: conditions `(field, op, value)` with `==, <, <=, >, >=` on top-level str, int,
  float or bool fields. A condition only matches values of its own type (int and float together),
  so missing fields and None never match. `order_by` takes `"field"` or `"-field"`; records
  without the order field are left out; ties are ordered by key. `limit` is 1..1000.
- `ctx.declare_index(kind, *equality_fields, *range_or_order_fields)` declares a query.
  Datastore needs it as a composite index (`framework-sanic export-indexes`) for any query on two
  or more fields. SQL creates a matching expression index at `setup`.
- Backends: memory (tests), SQLAlchemy (SQLite, PostgreSQL with JSONB) and Datastore (official
  `google-cloud-datastore` client in a thread pool; **VERIFY** against an async client under
  load). A custom adapter is selected with `STORAGE_ADAPTER=pkg.module:factory`.
- One contract suite (`framework_sanic.testing.repository_contract`) runs against every adapter.
- Each integration is its own deployment, so each may pick its own database.

### Connector (deliberately small)

```python
class Connector(Protocol):
    name: str
    auth: ConnectorAuth                 # OAuth2 (PKCE) or API key
    capabilities: set[str]
    def ui_manifest(self) -> UiManifest: ...
    async def search_entity(self, cred, query: EntityQuery) -> list[Entity]: ...
    async def get_entity(self, cred, entity_map: EntityMap) -> Entity: ...
    async def create_entity(self, cred, data: NewEntity) -> Entity: ...
    async def match_contact(self, cred, caller: Caller) -> ContactMatch: ...
    def map_error(self, exc: Exception) -> ConnectorError: ...
        # kinds: auth | rate_limited | not_found | transient | permanent
```

- Whatever else the listeners need (log a call, attach a recording, add a note) is the
  connector's own methods, called by its own listeners.
- `connectors/rest.py` provides `OAuth2Auth` and `RestConnector` (shared HTTP client, one quick
  retry of idempotent requests, pagination, status → `ConnectorError` mapping, `Retry-After`).
- Matching rules belong to the connector. `sdk.match_by_phone` and the phone helpers (E.164,
  national, leading zero) are optional utilities.
- A connector contract suite (`framework_sanic.testing.connector_contract`) must pass.

### Listeners (how integrations receive activity)

| Base class | Method | Receives |
|---|---|---|
| `CallListener` | `on_call(event) -> JobResult` | every call event for logging-enabled targets |
| `SmsListener` | `on_sms(event) -> JobResult` | every SMS event; `await event.sms_text()` decrypts the text |
| `NoteListener` | `on_note(principal, note) -> NoteOut` | `POST /notes` from the contact card |

- A listener is a feature: set `name` (and `scopes` if needed), register it under the
  `framework_sanic.features` entry point, enable it with `FEATURES=`. `configure(ctx)` adds jobs,
  schedules, routes, settings, indexes and company data. Each framework instance gets its own
  copy of every listener.
- `JobResult` is `ACK`, `retry(delay, reason, count_attempt=True)` or `deadletter(reason)`.
  `count_attempt=False` means "not yet" (waiting) and does not use up attempts.
- The framework provides plain models (`CallActivity.from_event`, `SmsActivity.from_event`,
  `NoteIn`) and stores nothing on the listener's behalf.

### SyncGuards (toolkit for sync jobs)

`parked(company)`, `rate(msg)`, `credential(company, user)` / `check(msg, user)`,
`lease(key) -> token | None` and `release(key, token)`, `on_error(exc, msg, cred)` (maps
`ConnectorError` to retry, park or deadletter). Leases carry a per-holder token so an expired
holder cannot release someone else's lease.

### Idempotency

Deterministic keys everywhere. Each logged activity is mapped to its external record with an
`EntityMap` row keyed by the activity key (`dialpad:<company>:call|sms|note:<id>`), and the
activity key is also stored on the external record. Before creating, a sync checks the local map,
then searches the external system by that reference, so retries never duplicate.

## 5. Data model

Framework kinds:

| Kind | Key | Notes |
|---|---|---|
| Company | `company_id` | tenant root |
| DialpadCredential | `company_id` + `credential_id` | primary and fallback; `version` for compare-and-set refresh; tokens encrypted |
| ExternalCredential | `company_id` + `scope(user\|company)` + `user_id` | tokens encrypted; marked bad only when the provider rejects the token |
| User | `company_id` + `dialpad_user_id` | `is_admin` **VERIFY** |
| Target | `company_id` + `target_type` + `target_id` | parent refs, `override`, denormalized `effective_enabled` |
| Subscription | `company_id` + `target_type` + `target_id` + `kind` | Dialpad subscription id |
| EntityMap | `company_id` + `dialpad_id` | caller (`phone:` / `contact:`) → CRM contact, or activity key → external record |
| Outbox | per unit of work | follow-up jobs; relayed if a publish was lost |
| AuditEvent | | connects, disconnects, config changes, deletions |

Integration kinds are the integration's own (HubSpot: `LoggedCall` extends `CallActivity` with
fold ranks and sync state; `LoggedSms`). Listeners declare them with `ctx.declare_company_data`,
so uninstall deletes them. Retention is the integration's setting (HubSpot: 90 days).

## 6. Event processing

Webhook endpoint: `POST /webhooks/dialpad/{company_id}/events`, an HS256 JWT body signed with a
per-company secret derived from `WEBHOOK_SECRET_KEY` (**VERIFY** format). Dialpad retry facts:
5 s timeout, 5xx retried, 4xx/429 not retried, call events about 3 retries (~14 s), SMS about 10
retries (~4 h), retries may arrive out of order, subscriptions are not auto-disabled.

The framework parses `CallEvent` / `SmsEvent`, drops operator legs when the group target's
logging is enabled (`entry_point_target`, **VERIFY**), checks the target's `effective_enabled`
and delivers to each listener. The rest is the listener's job. HubSpot's reference listeners:

- **Calls:** fold every event into one `LoggedCall` per `call_id`, order-independent (the
  highest-ranked event wins each field; terminal states `hangup`, `missed`, `voicemail` never
  regress; recordings are a set). The fold and its sync job are written in one unit of work.
  The sync job matches the contact, creates or updates the HubSpot call and links recordings
  (never copies audio). Property tests shuffle, duplicate and drop events; the result must be
  identical.
- **SMS:** insert once by SMS id, skip if the first status seen is `failed`, then a sync job
  decrypts the text just long enough to send it. Subscription: `direction=all`,
  `include_internal=false`, `status=false`. Group SMS policy **VERIFY**.
- **Notes:** `POST /notes` (client-generated `note_id`) queues a job that waits for the call to be
  logged (uncounted retries, gives up after 24 hours), then adds the note.
- Rules are HubSpot settings: `HUBSPOT_CALLS_*` and `HUBSPOT_SMS_*` (directions, skipped states,
  minimum duration, recordings, unknown callers **VERIFY**, retention).

Queue retries: the attempt number and the "not before" time travel inside the message, so
attempts are counted the same on memory and Pub/Sub, and waiting never counts as a failure.

## 7. Dialpad integration

- Target hierarchy: company → office → {user, department, call center}, built from list calls.
  `LOGGABLE_TARGET_TYPES` selects which types get subscriptions (list endpoints **VERIFY**).
- Logging is on by default, inherited down the hierarchy, and can be overridden per target
  (`PUT /admin/targets/{type}/{id}/logging`). `effective_enabled` is recomputed under a
  per-company lease, so a background recompute cannot overwrite an admin's change.
- Subscriptions are per target and per event kind, never per company, and only for kinds an
  enabled listener handles. The reconciler diffs desired against existing subscriptions; it is
  idempotent and safe to run concurrently. A weekly sweep, staggered across companies, catches
  drift. Change-log events only trigger reconciles (`ChangeLogSource`, no-op adapter,
  **VERIFY**).
- API budget: `DIALPAD_RATE_LIMIT_PER_MINUTE` (1,200, **VERIFY** burst vs sustained) per
  company, a Redis token bucket shared by the reconciler and UI calls (UI on a higher lane).
- Subscriptions are never auto-disabled. A broken connection parks the company instead.

## 8. Authentication

Admin connect (Dialpad OAuth): `GET /session` reports the app state; `GET /oauth/dialpad/start`
stores `state` + PKCE verifier in Redis; the callback stores the tokens encrypted as a
`DialpadCredential`. Multiple admins can connect (primary, then fallback). Refresh uses
compare-and-set on `version`, and the result of `upsert` (not a flag set inside the merge)
decides who won.

UI gating: the iframe URL carries an `id_token` (`IdTokenVerifier`, format **VERIFY**). The UI
exchanges it for a session token (`SESSION_TTL_SECONDS`, 15 minutes) kept in memory only. The
token can be renewed until `SESSION_MAX_AGE_SECONDS` (8 hours) after the Dialpad login; then the
iframe must log in again. Revocations live in Redis.

External service: user credential first, then company. UI reads use the acting user's; sync jobs
use the activity owner's, falling back to the company's. Only a rejected token (`auth` error)
marks a credential bad; outages are retried. Required Dialpad scopes come from enabled listeners
and features (`ctx.require_scopes`); the service refuses to start if they are not in
`DIALPAD_SCOPES`.

## 9. Fairness, scale, reliability

Targets: 3,000 companies × 1,000 users; ≥30M calls/day; ≤30M SMS/day (**VERIFY**).

- Queues sharded by company (`SHARD_COUNT`); topics `events` (listener handlers), `sync`
  (follow-up jobs) and `control` (reconciles).
- Per-company and per-connector token buckets; per-company concurrency caps
  (`PER_COMPANY_CONCURRENCY`) so one noisy company cannot starve the others.
- Priority lanes: UI > live events > background sweeps.
- Broken connections park the company's sync jobs while intake keeps flowing; they resume on
  reconnect.
- External 429s honor `Retry-After` with backoff, inside the service.
- Concurrent contact creation for the same caller is serialized by a lease, so one contact is
  created.
- Load test: quiet companies stay fast while one company floods.

## 10. HTTP layer

Middleware order: request id and logging → error mapping → CORS / CSP (`frame-ancestors` for
Dialpad origins) → webhook signature (webhook routes) → session / `id_token` auth → company
context → rate limit → cache headers.

Auth modes: `none`, `webhook`, `session`, `admin`. Core routes:

- `POST /webhooks/dialpad/{company_id}/events`
- `GET /session`, `POST /session/logout`
- `GET /oauth/dialpad/start`, `GET /oauth/dialpad/callback`
- `GET /oauth/external/start`, `GET /oauth/external/callback`
- `GET /contact`, `GET /contact/search`, `POST /contact/rematch`, `POST /contact`
  (`dialpad_ref` must be `phone:…` or `contact:…`; rematch affects future activities only)
- `GET /admin/targets`, `PUT /admin/targets/{type}/{id}/logging`, `POST /admin/reconcile`,
  `POST /admin/uninstall`
- `GET /healthz`, `GET /readyz`
- Routes added by features, such as `POST /notes` from a note listener.

The black-box `http_contract` suite runs against the Sanic adapter and the neutral dispatcher.
FastAPI would be one more adapter file. `framework-sanic export-schemas` generates the UI's
TypeScript client from the route models.

## 11. Features and extension

A feature registers through `FeatureContext`: `on_event`, `route`, `job`, `schedule`,
`require_scopes`, `declare_config`, `settings` (its own env-prefixed settings class),
`declare_index` and `declare_company_data`. The platform feature (always on) runs the reconciler,
outbox relay, expiry purge, weekly sweep and change-log polling. Integration features and
listeners are enabled with `FEATURES`, share credentials and the repository through `Services`,
and cannot import each other.

Adding an integration: `framework-sanic new <name>` scaffolds a package (connector, call / SMS /
note listeners, tests); `framework-sanic check <path>` is the gate (SDK boundary, plugins,
wiring, contract suites, tests); `framework-sanic dev --connector <name>` runs it locally.

## 12. Embedded UI (Vue 3)

- Vue 3 `<script setup>`, TypeScript, Vite, Pinia, TanStack Vue Query, pnpm workspace.
- Packages: `ui-shell` (iframe bootstrap, session, postMessage bridge), `ui-kit`, `api-client`
  (generated), `connector-ui-*`.
- The contact card is driven by the connector's `ui_manifest()`; no connector-specific code.
- Call context arrives by `postMessage` (shape **VERIFY**); `event.origin` is checked.
- Notes appear only when a note listener is enabled; client-generated ids make retries safe.
- Served by the service at `/app/` (`SERVE_UI_DIR`) or hosted statically (**VERIFY**).

## 13. Configuration

Framework (env, `.env` locally, terraform in beta / production): `DIALPAD_CLIENT_ID`,
`DIALPAD_CLIENT_SECRET`, `DIALPAD_SCOPES`, `DIALPAD_IFRAME_URL`, `DIALPAD_API_BASE`,
`DIALPAD_FRAME_ORIGINS`, `DIALPAD_CALL_STATES` (default `hangup,missed,voicemail,recording`),
`LOGGABLE_TARGET_TYPES`, `DIALPAD_RATE_LIMIT_PER_MINUTE`, `PLATFORM_ADAPTER`,
`CHANGELOG_LIFECYCLE_ACTIONS`, `CONNECTOR`, `FEATURES`, `CONNECTOR_RATE_LIMIT_PER_MINUTE`,
`STORAGE_ADAPTER` (memory | sqlalchemy | datastore | `pkg.module:factory`), `DATABASE_URL`,
`DATASTORE_PROJECT`, `DATASTORE_NAMESPACE`, `QUEUE_ADAPTER`, `PUBSUB_PROJECT`, `PUBSUB_PREFIX`,
`CACHE_URL`, `KMS_KEY`, `LOCAL_MASTER_KEY`, `SHARD_COUNT`, `PURGE_*`, `PUBLIC_BASE_URL`,
`UI_ORIGINS`, `WEBHOOK_SECRET_KEY`, `SESSION_SECRET`, `SESSION_TTL_SECONDS`,
`SESSION_MAX_AGE_SECONDS`, `EMBEDDED_WORKERS`, `WORKER_CONCURRENCY`, `PER_COMPANY_CONCURRENCY`,
`UI_RATE_LIMIT_PER_MINUTE`, `CONTACT_CACHE_SECONDS`, `SWEEP_TICK_SECONDS`, `LOG_LEVEL`,
`SERVE_UI_DIR`.

Integration settings use their own prefix (HubSpot: `HUBSPOT_*`, `HUBSPOT_CALLS_*`,
`HUBSPOT_SMS_*`).

Backend client libraries are extras: `gcp` (Datastore, Pub/Sub, KMS), `redis`, `sql`,
`dev-stack`. Selecting an adapter whose extra is missing fails at startup with the install line.
Local stack: `framework-sanic dev` (SQLite + Redis, fake Dialpad, demo data), or docker compose
with the Datastore and Pub/Sub emulators, Redis, the fake Dialpad (replays duplicated and
shuffled webhooks) and fake listeners.

## 14. Privacy and security

- Contact details are read live; activity records expire after the integration's retention
  period; no transcripts; SMS text never stored.
- Tokens use envelope encryption (`KeyProvider`: GCP KMS in production, local key in dev) and
  encrypted fields are never indexed.
- Uninstall deletes all company data, including every kind features declared, and keeps an audit
  log.
- Logging is on by default; the UI shows an admin notice.

## 15. Current state and next work

Built and gated: everything above, with contract suites (repository, queue, cache, connector),
property tests, black-box HTTP tests, a load test, ruff, mypy (`--strict` on core) and
`framework-sanic check integrations/hubspot`.

Not yet done:

- Datastore, Pub/Sub and PostgreSQL have not run against real services (contract suites run on
  the emulators / `TEST_DATABASE_URL` when available). Multi-field range queries on Datastore
  depend on its multi-inequality support (**VERIFY**).
- Terraform and CI assume Datastore. Per-integration storage choice (Cloud SQL wiring, roles,
  `DATABASE_URL` secret, index step only for Datastore) is pending.
- Paging is limit/offset; add cursors if deep paging is needed.
- No migration runner: `schema_version` is stored but not read, so model changes must stay
  backward compatible.

## 16. Assumptions to verify

Tracked in `docs/open-questions.md` (19 items), including: `is_admin` and the `id_token` format;
the department-leg `operator` shape and `entry_point_target`; the default call states; group SMS;
unknown callers; the HubSpot SMS object and scopes; the change-log API; data residency; the
Datastore client under load; the 1,200 req/min enforcement; the list endpoints; the webhook
format; UI hosting; the `postMessage` shape; the scale numbers; the uninstall signal.

## 17. Non-goals

- The HubSpot SMS campaign (specified separately).
- A timeline of calls or SMS in the UI.
- Rematching past activities.
- Automatically disabling subscriptions on failures.
- Custom data fields from call events.

## 18. Definition of done (for any change)

Contract suites green on every adapter you can run, property tests green, import lint and SDK
boundary green, `mypy --strict` on core, `framework-sanic check` passing for every integration,
the docker compose replay working end to end, and the README, `docs/system-overview.md` and
`docs/open-questions.md` updated with the change.
