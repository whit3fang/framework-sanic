# Integration Framework: Coding Agent Brief

You are building a reusable integration framework that embeds in Dialpad and connects it to external systems (CRMs, ticketing). HubSpot is only the first example connector. Build the framework first; connectors plug into it.

The full design is in the "Integration Framework: Technical Specification" doc. This brief is the self-contained version for implementation. Where something is marked **VERIFY**, it is an unconfirmed assumption: isolate it behind a port or config value, do not hard-code it, and list it in `docs/open-questions.md`.

## 1. What the system does

1. Authenticates with Dialpad (admin OAuth connect per Company; per-user `id_token` gating for the embedded UI).
2. Authenticates with the external service (per user first, then company-level credential).
3. Subscribes to Dialpad call and SMS webhooks for each loggable target.
4. Logs calls and SMS as activities in the external service. One activity per call leg (`call_id`).
5. Serves an embedded Vue 3 UI (in a Dialpad iframe) showing the matched external contact, allowing rematch and adding notes.
6. Hosts integration-specific features (for example a HubSpot SMS campaign, specified separately, not part of this build).

The UI shows no call or SMS event history. It shows contact info from the external service, lets the user rematch the contact, and lets them add notes.

## 2. Hard rules

- Core domain code is plain Python with **no web framework imports**. `sanic` and `sanic_ext` may be imported only in `http/adapters/sanic.py`. Add a lint/import-check test that fails the build otherwise.
- Everything external sits behind a port (interface). Adapters are swappable by config.
- Handlers are framework-neutral: `async def handler(req: PydanticModel, ctx: RequestContext) -> PydanticModel`, registered via a `@route(method, path, auth=...)` decorator. The Sanic adapter translates.
- The webhook accept path is buffer-first: verify signature, encrypt SMS text, publish to the durable queue, return 2xx. It must work with Redis and the database down. Never return 4xx/429 for a deliverable event (Dialpad does not retry 4xx).
- Redis is never the source of truth. No refresh tokens in Redis. Redis holds OAuth state/PKCE, leases, caches, rate-limit buckets, dedupe fast path, session revocation.
- Never store SMS text. It is encrypted inside the queue message and dropped after sync. Never store call transcripts.
- Never use `v-html` with external data in the UI.
- Python 3.12+, Pydantic v2, async everywhere. Type hints and `mypy --strict` on the core.

## 3. Architecture and package layout

```
src/
  core/                    # no framework imports
    domain/                # entities, value objects, events
    ports/                 # ParentPlatform, Repository, UnitOfWork, TokenStore,
                           # KeyProvider, Queue, Scheduler, Cache, Connector,
                           # IdTokenVerifier, ChangeLogSource
    services/              # activity_merge, subscription_reconciler, credential_service,
                           # contact_service, notes_service, rate_limit
    features/              # FeatureContext, feature registry, activity_sync
  adapters/
    dialpad/               # ParentPlatform impl, webhook verification
    storage/               # datastore/, sqlalchemy/, memory/
    queue/                 # pubsub or cloud tasks, memory
    cache/                 # redis, memory
    kms/                   # gcp_kms, local
  http/
    routes.py              # @route registry
    middleware.py          # framework-neutral pipeline
    adapters/sanic.py      # only place that imports sanic
  connectors/
    hubspot/
      connector.py
      features/sms_campaign/   # out of scope for this build
  workers/                 # stage1, stage2, reconciler, sweeper
ui/                        # pnpm monorepo: ui-shell, ui-kit, api-client,
                           # connector-ui-hubspot, feature-ui-*
tests/
  contract/                # repository, queue, cache, connector suites
  property/                # event merge properties
  http_contract/           # black-box tests over the HTTP surface
```

Data flow:

- Event path: Dialpad webhook → accept path → durable queue (sharded by company hash) → stage 1 (merge into activity record, write outbox in same UnitOfWork) → stage 2 sync workers (connector call, rate limited) → external service.
- UI path: iframe → Vue app → HTTP layer → connector live reads (using the acting user's credential).

## 4. Ports and contracts

```python
class Repository(Protocol):
    async def get(self, kind: str, key: Key) -> Record | None: ...
    async def upsert(self, kind: str, key: Key,
                     merge_fn: Callable[[Record | None], Record | None]
                     ) -> Literal["created", "updated", "unchanged", "ignored"]: ...
    async def query(self, kind: str, **filters) -> AsyncIterator[Record]: ...
    def unit_of_work(self) -> UnitOfWork: ...   # includes outbox writes

class Connector(Protocol):
    auth: ConnectorAuth                          # OAuth or API key description
    capabilities: set[str]
    def ui_manifest(self) -> UiManifest: ...
    async def search_entity(self, cred, query: EntityQuery) -> list[Entity]: ...
    async def get_entity(self, cred, ref: EntityRef) -> Entity: ...
    async def create_entity(self, cred, data: NewEntity) -> Entity: ...
    async def upsert_call(self, cred, activity: CallActivity) -> ExternalRef: ...
    async def log_sms(self, cred, activity: SmsActivity) -> ExternalRef: ...
    async def attach_recording(self, cred, ref: ExternalRef, url: str) -> None: ...
    async def add_note(self, cred, ref: ExternalRef, note: Note) -> None: ...
    async def find_by_reference(self, cred, activity_key: str) -> ExternalRef | None: ...
    def map_error(self, exc: Exception) -> ConnectorError: ...
```

Connector idempotency: store the activity key as an external reference on the external record; `find_by_reference` is used before creating, so retries never duplicate.

Contact matching helpers (E.164, national, leading-zero) are provided as utilities. They are not enforced; each connector decides.

Storage rules:

- Deterministic keys (no random ids for events or activities).
- Every record has `schema_version`.
- Call/SMS activity records carry a TTL of 90 days (`ACTIVITY_TTL_DAYS`).
- Denormalize `effective_enabled` on targets so queries do not walk the hierarchy.
- One repository contract test suite runs against every adapter (memory, Datastore emulator, SQLAlchemy).
- Datastore adapter: official `google-cloud-datastore` client behind the port. Do not use Cloud NDB. **VERIFY**: benchmark thread-pool wrapper vs async client at target load.

## 5. Data model (keys)

| Kind | Key | Notes |
|---|---|---|
| Company | `company_id` | Dialpad Company; tenant root |
| DialpadCredential | `company_id` + `credential_id` | multiple per company; primary and fallback; `version` for compare-and-set refresh |
| ExternalCredential | `company_id` + `scope(user\|company)` + `user_id?` | encrypted via KeyProvider |
| User | `company_id` + `dialpad_user_id` | maps to external user id; `is_admin` **VERIFY** |
| Target | `company_id` + `target_type` + `target_id` | hierarchy parent refs, `override`, `effective_enabled` |
| Subscription | `company_id` + `target_type` + `target_id` + `kind(call\|sms)` | stores Dialpad subscription id; one per target and kind |
| CallActivity | `company_id` + `call_id` | one per leg; folded state |
| SmsActivity | `company_id` + `sms_id` (int64) | insert-once |
| ContactLink | `company_id` + `dialpad_contact_ref` | rematch target; forward-only |
| Note | `company_id` + client-generated `note_id` | idempotent create |
| Outbox | per UnitOfWork | drives stage 2 |

## 6. Event processing rules

Webhook endpoint: `POST /webhooks/dialpad/{company_id}/events`. Verify HS256 JWT with the per-company secret. Parse into `CallEvent` or `SmsEvent` (Pydantic). Dialpad retry facts: 5 s timeout, 5xx retried, 4xx/429 not retried, call events 3 retries (~14 s), SMS 10 retries (~4 h), retries may arrive out of order, subscriptions are not auto-disabled.

Call activity is a fold over events ordered by `event_timestamp`:

- Additive fields (recording URL, etc.) merge at any time.
- Terminal states: `hangup`, `missed`, `voicemail`. A later non-terminal event never regresses a terminal state.
- Dedupe key `hash(call_id, state, event_timestamp)` is only a fast path (Redis). Correctness comes from the atomic `upsert` merge, so duplicates and reordering are safe without Redis.
- One activity per `call_id`. Transfers produce new ids and therefore new activities.
- Department/call-center calls: the department leg is logged. When the group target's logging is enabled (resolved via `entry_point_target`), drop the operator-leg events. The activity owner comes from the `operator` JSON on the department leg (**VERIFY** payload shape; no real sample exists, so parse defensively and keep a fixture).
- Recording URLs do not expire; link them, do not copy audio.

SMS: insert-once keyed by `id`. Skip if the first seen `message_status` is `failed`. Dialpad only sends events for successfully sent SMS. Subscribe with `direction=all`, `include_internal=false`, `status=false`. `contact_group` policy is open (**VERIFY**).

Tests (property-based): shuffle, duplicate, and drop non-terminal events of generated call sequences; the final folded activity must be identical.

## 7. Dialpad integration

- Terminology: Company (tenant), Target (user, department, call center, office, company), User, OauthApp.
- Target hierarchy: company → office → {user, department, call center}, built from list calls joined on parent references. `LOGGABLE_TARGET_TYPES` (config) selects which types get subscriptions.
- Logging is on by default. Configuration is inherited down the hierarchy and can be overridden per target. `effective_enabled` is recomputed when a config or hierarchy changes.
- Subscriptions are per target, not per company (company-level subscription does not cover users' calls).
  - Call: `call_states` (from `DIALPAD_CALL_STATES`), `enabled`, `endpoint_id`, `group_calls_only`, `target_type`, `target_id`.
  - SMS: see section 6.
- No duplicate subscriptions: reconciler lists existing subscriptions per company and diffs against desired state (create, update, enable/disable). Idempotent and safe to rerun.
- Weekly staggered sweep (scheduled task) runs the reconciler for every company; jitter across the week.
- Change-log events are refresh triggers only (target created or deleted → enqueue a reconcile for that company). Keep `ChangeLogSource` as a placeholder port with a no-op adapter; the exact API is unknown.
- API budget: 1,200 requests/min per company. Enforce with a Redis token bucket per company; reconciler and UI calls share it, with UI calls on a higher-priority lane.
- Do not auto-disable subscriptions on repeated failures. Park a company's invalid connection instead (see section 9).

## 8. Authentication

Admin connect (Dialpad OAuth):

1. Admin opens the app; `GET /session` reports the app state (`needs_admin_connect`, `ready`, `forbidden`, ...).
2. `GET /oauth/dialpad/start` creates `state` + PKCE (S256) verifier, stores both in Redis with a short TTL, redirects.
3. Callback validates `state`, exchanges the code, stores access and refresh tokens (encrypted) as a new `DialpadCredential`. Refresh tokens never expire.
4. Multiple admins can connect. Credentials are ordered primary then fallback. Refresh uses compare-and-set on `version`. A validity probe marks a credential bad; calls fall to the next credential.

UI per-user gating:

- The Dialpad iframe URL carries an `id_token` for the current user. Verification sits behind the `IdTokenVerifier` port (token format **VERIFY**).
- Middleware verifies it, resolves the User and Company, and exposes the result on `RequestContext`. The UI exchanges it for a short-lived session token held in memory only (never localStorage).
- Session revocation list lives in Redis.

External service:

- Credential resolution is user-first, then company. UI reads use the acting user's credential. Background sync uses the activity owner's credential, falling back to the company's.
- Scopes: `recordings_export`, `message_content_export` (and `:all`), `calls:list`, `change_log`. At startup, validate that scopes declared by enabled features are included in `DIALPAD_SCOPES`; refuse to start otherwise.

## 9. Fairness, scale, reliability

Targets: 3,000 companies × 1,000 users; at least 30M calls/day; at most 30M SMS/day (**VERIFY** against real usage).

- Queues are sharded by company hash (`SHARD_COUNT`).
- Two stages: stage 1 merge is cheap and never calls external services; stage 2 syncs externally.
- Per-company and per-connector Redis token buckets; per-company concurrency caps so one noisy company cannot starve others.
- Priority lanes: UI-initiated > live call terminal events > backfill/sweep.
- Invalid or revoked connections: park that company's stage-2 messages (keep stage 1 flowing), alert, resume on reconnect.
- Autoscale workers on oldest-message age, not queue depth.
- External 429s: honor `Retry-After`, back off with jitter, retry in the microservice (not by failing the Dialpad webhook).

## 10. HTTP layer

Middleware pipeline (framework-neutral, in this order): request id and logging → error mapping → CORS/CSP (`frame-ancestors` for Dialpad origins) → webhook signature check (webhook routes only) → id_token/session auth → company context → rate limit → cache headers.

Route auth modes: `none`, `webhook`, `session`, `admin`. Routes use Pydantic models for input and output. Core routes:

- `POST /webhooks/dialpad/{company_id}/events`
- `GET /session`
- `GET /oauth/dialpad/start`, `GET /oauth/dialpad/callback`
- `GET /oauth/external/start`, `GET /oauth/external/callback`
- `GET /contact?dialpad_ref=...` (live read, cached briefly in Redis)
- `POST /contact/rematch` (forward-only: affects future activities, never past ones)
- `POST /contact` (create in the external service)
- `POST /notes` (client-generated `note_id`; appended to the call activity linked to the contact)
- `GET /healthz`, `GET /readyz`

FastAPI migration path: add `http/adapters/fastapi.py`, run the black-box `http_contract` tests against both adapters, switch by config.

## 11. Features and extension model

A feature registers through `FeatureContext`: routes, job handlers, schedules, subscriptions, declared scopes, declared config keys, and storage indexes. `activity_sync` is a platform feature (always on). Integration features (for example `connectors/hubspot/features/sms_campaign/`) are enabled through `FEATURES` and share credentials and the Repository via the context. Features cannot import each other.

## 12. Embedded UI (Vue 3)

- Vue 3 Composition API with `<script setup>`, TypeScript, Vite, Pinia, TanStack Vue Query, pnpm workspace.
- Packages: `ui-shell` (iframe bootstrap, session, postMessage), `ui-kit`, `api-client` (generated from the Pydantic models), `connector-ui-*`, `feature-ui-*`.
- The contact card is manifest-driven: the connector's `ui_manifest()` lists fields, labels, and link-outs; the UI renders it without connector-specific code.
- Call context (the number, call id) arrives via `postMessage` from Dialpad; validate `event.origin`.
- Notes use client-generated IDs so retries are safe. Rematch shows a search box and a confirmation of forward-only behavior.
- Session token in memory only. Escape all external data. CSP `frame-ancestors`.
- Open: static hosting vs served by the service (**VERIFY**).

## 13. Configuration (env / terraform)

`DIALPAD_CLIENT_ID`, `DIALPAD_CLIENT_SECRET`, `DIALPAD_SCOPES`, `DIALPAD_IFRAME_URL`, `DIALPAD_CALL_STATES` (default proposal: `hangup,missed,voicemail,recording`), `LOGGABLE_TARGET_TYPES`, `CONNECTOR`, `FEATURES`, `STORAGE_ADAPTER`, `QUEUE_ADAPTER`, `CACHE_URL`, `KMS_KEY`, `SHARD_COUNT`, `ACTIVITY_TTL_DAYS` (default 90), `CHANGELOG_LIFECYCLE_ACTIONS`.

Local development uses `.env`; beta and production use terraform. Provide `docker compose` (or equivalent) with the Datastore emulator, Redis, and a local queue, a fake Dialpad server that can replay webhooks (including duplicated and shuffled sequences), and a fake connector.

## 14. Privacy and security

- Contact details are stored while connected; call and SMS metadata for 90 days; no transcripts; SMS text never stored.
- Tokens encrypted at rest with envelope encryption via `KeyProvider` (GCP KMS in prod, local key in dev).
- Delete all company data on uninstall; keep an audit log of connects, disconnects, config changes, and deletions.
- Logging is on by default, so surface an admin notice in the UI.

## 15. Build order (each step ends with a passing gate)

1. Skeleton, ports, memory adapters, import-lint rule. Gate: lint test and repository contract suite on memory.
2. Datastore adapter (and SQLAlchemy if time allows). Gate: same contract suite passes on the emulator.
3. HTTP layer: route registry, middleware, Sanic adapter, `/healthz`. Gate: `http_contract` suite.
4. Dialpad adapter and auth: admin OAuth, credentials with compare-and-set refresh, `IdTokenVerifier`, `/session`. Gate: fake Dialpad flow test.
5. Webhook accept path and queue. Gate: works with cache and DB stopped; signature failures covered.
6. Stage 1 merge. Gate: property tests (shuffle, duplicate, drop) for calls; insert-once tests for SMS.
7. Target hierarchy, reconciler, weekly sweep, change-log placeholder. Gate: no duplicate subscriptions across repeated runs and concurrent runs.
8. Connector contract and a fake connector; stage 2 sync with rate limits and idempotent external references. Gate: duplicate stage 2 delivery creates one external record.
9. Fairness: shard queues, token buckets, concurrency caps, parking. Gate: load test with one noisy company; others stay within latency target.
10. UI shell, contact card, rematch, notes. Gate: component tests and an end-to-end test in the fake Dialpad harness.
11. HubSpot connector. Gate: sandbox verification list (see below).

## 16. Assumptions to verify (flag with `# VERIFY` in code)

- `is_admin` on the user object; `id_token` format and claims.
- Department-leg `operator` JSON shape and `entry_point_target` semantics.
- Default call-state list; whether group SMS (`contact_group`) should log.
- Default behavior for unknown callers (create contact or not).
- HubSpot object used for SMS activity.
- Change-log API shape and scopes.
- Data residency requirements.
- Datastore client choice under load.
- Real 1,200 req/min enforcement behavior (burst vs sustained).

## 17. Non-goals

- The HubSpot SMS campaign (specified separately).
- A timeline of calls or SMS in the UI.
- Rematching past activities.
- Automatic disabling of subscriptions on failures.
- Custom data fields from call events.

## 18. Definition of done

Contract suites green on every adapter, property tests green, import-lint green, `mypy --strict` on core, a local `docker compose up` that replays a call sequence end to end into the fake connector, and a README covering setup, config, and how to add a connector and a feature.
