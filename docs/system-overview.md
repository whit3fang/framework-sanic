# System overview

A high-level walkthrough of the integration framework: what it solves, how it is shaped, and
where the hard parts are. It is written to be talked through in about 15 minutes. Details live
in the code and the [README](../README.md), and unconfirmed assumptions are listed in
[open-questions.md](open-questions.md).

Suggested emphasis: spend most of the time on sections 3–6. That's where the hard
distributed-systems reasoning is. Keep the others to about a minute each.

## 1. The problem and the constraints

Dialpad integrations with external systems like CRMs all need the same foundation: connect
accounts, subscribe to call and SMS events, receive them reliably, and show the matched contact
inside an embedded panel. What each integration then *does* with an event varies. So the
framework owns receiving and delivering events, and each integration decides what they mean.
HubSpot is the first integration built on it: it logs calls, SMS and call notes.

The constraints that shape every decision:

- **Scale:** about 3,000 companies, roughly 30M calls a day.
- **Dialpad's webhook rules:** a 5-second timeout, and no retry on 4xx responses.
- **Privacy:** SMS text is never stored and transcripts are dropped.

## 2. Architecture: ports and adapters

- The core is plain Python. Everything external (storage, queue, cache, KMS, Dialpad, the CRM)
  sits behind an interface (a "port"), and config picks the implementation (an "adapter").
- The web framework is isolated to one file (`http/adapters/sanic.py`), and a lint test
  enforces that. Moving from Sanic to FastAPI means adding one adapter and running the same
  black-box HTTP tests against it.
- **Plain install, optional backends:** the package works on in-memory adapters alone. Each
  production backend's client library is an extra (`gcp`, `redis`, `sql`), and selecting an
  adapter whose extra is missing fails at startup with the install line to run.
- **Why:** each adapter can be tested on its own, and integrations can be added without
  touching the core.

```
integration package (e.g. framework_sanic_hubspot)      uses only framework_sanic.sdk
  connector  ── auth, contact search/match/create, its own CRM writes
  listeners  ── what calls, SMS and notes become in the CRM
──────────────────────────────────────────────────────────────────────────────────────
framework     intake ─ queue ─ workers ─ storage ─ credentials ─ subscriptions ─ UI/HTTP
```

## 3. The event pipeline: the framework receives, listeners decide

This is the heart of the system, and the most important boundary in it.

```
            ┌──────────────────────── framework ────────────────────────┐   ┌─ integration ─┐
Dialpad ──► │ verify ─► encrypt SMS text ─► fan out ─► queue ─► worker ─│─► │   listener    │
webhook     └───────────────────────────────────────────────────────────┘   └───────────────┘
```

**What the framework guarantees:**

- **Buffer-first accept path:** verify the signature, encrypt the SMS text, put the event on a
  durable queue, return 2xx. It keeps working when the database and Redis are down.
- **Fan-out:** one queue message per enabled listener for that event kind, so each listener
  retries independently. A failing listener never makes another see an event twice.
- **Delivery:** at least once. The listener's result (acknowledge, retry later, deadletter)
  drives the queue.
- **Subscriptions follow listeners:** Dialpad subscriptions are created only for event kinds an
  enabled listener handles. With none enabled, nothing is subscribed.
- **A toolkit for processing:** atomic repository upserts, a transactional outbox, and
  `SyncGuards` (parking, credentials, rate budgets, leases, error mapping).

**What the integration decides.** The framework ships three listener base classes and plain
models, and stores nothing on the integration's behalf:

| Listener | Receives | Model |
|---|---|---|
| `CallListener.on_call` | every call event | `CallActivity.from_event(...)` |
| `SmsListener.on_sms` | every SMS event; `await event.sms_text()` decrypts the text | `SmsActivity.from_event(...)` |
| `NoteListener.on_note` | `POST /notes` from the contact card (the notes panel appears only when one is enabled) | `NoteIn` |

Whether a call becomes a CRM activity, a ticket update or nothing is the listener's code.
Listeners are features: they're registered by entry point and enabled with `FEATURES=`, and each
framework instance sets up its own copy of each one.

**HubSpot's listeners are the reference** (`integrations/hubspot/.../listeners/`):

- **`hubspot.calls`:** a cheap fold of call events into one stored record (never calls
  HubSpot), written together with its sync job in one transaction (the outbox). The sync job
  then matches the contact, creates or updates the HubSpot call and attaches recordings.
- **`hubspot.sms`:** store the SMS metadata once, then a sync job decrypts the text just long
  enough to send it. The text is never stored.
- **`hubspot.notes`:** queue a job that waits until the call is logged (notes are often typed
  during the call), then adds the note. Waiting doesn't use up retry attempts; it gives up after
  24 hours.
- Their rules are HubSpot settings, not framework ones: `HUBSPOT_CALLS_*` and `HUBSPOT_SMS_*`
  (directions, skipped states, minimum duration, recordings, unknown callers, retention).

## 4. Correctness under messy delivery

The framework's contract is at-least-once, so listeners must be idempotent (safe to run twice).
HubSpot's call listener shows how:

- Dialpad retries arrive out of order and duplicated. The call record is an order-independent
  fold: the highest-ranked event wins each field, and a terminal state like hangup can never be
  undone by a late event. Property-based tests shuffle, duplicate and drop events, and the final
  record must come out identical.
- **Idempotency everywhere:** deterministic keys, insert-once SMS, client-generated note IDs,
  and the Dialpad activity key stored on each CRM record. Before creating anything, a sync looks
  at the local map first and then searches HubSpot, so a retry never creates a duplicate.
- **The outbox:** the activity change and its follow-up job are written in one transaction. A
  relay republishes any job whose publish was lost in a crash, so a sync can be late but never
  lost.
- **Leases** (short Redis locks) stop two jobs from syncing the same activity at once. Each
  holder gets its own token, so a slow job whose lease expired can't free the lease someone
  else took after it.
- **Retries are counted correctly on every queue.** Pub/Sub can't tell a failure from a "not
  yet", so the attempt number and the "not before" time travel inside the message. Waiting
  (parking, rate limits, per-company caps) doesn't count as a failed attempt.
- **Redis is only a fast path.** Correctness comes from atomic database upserts with version
  checks (compare-and-set).

## 5. Multi-tenancy, fairness and scale

These are framework guarantees, so every integration gets them for free:

- Queues are sharded by company. Each company has its own token bucket for API calls and a cap
  on how many of its jobs run at once.
- Priority lanes: UI requests first, then live events, then background sweeps.
- A company with a broken connection is "parked": its external sync waits without failing,
  while event intake keeps flowing.
- A CRM outage doesn't disconnect anyone: only a rejected token marks a credential bad.
  Network errors and 5xx/429 responses are retried later.
- The load test shows quiet companies finishing in milliseconds while a noisy company's backlog
  takes seconds.

## 6. Data: one storage port, any database

- **Everything is a record:** an entity class gives a `KIND` (like a table name) and a
  deterministic `key()`, and is stored as a JSON document. Framework data and an integration's
  own data (HubSpot's `LoggedCall`) go through the same small `Repository` port. A new
  kind needs no table and no migration.
- **Writes are atomic read-modify-writes** (`upsert(kind, key, merge_fn)`), with small
  multi-record transactions for the outbox.
- **Queries:** equality filters, ranges, sorting, paging (`find`) and `count`, with rules that
  are the same on every backend and checked by one contract suite. `ctx.declare_index` declares
  the queries a feature runs: Datastore gets composite indexes from it, and SQL gets matching
  expression indexes (verified with the query plan on SQLite).
- **Backends:** memory (tests), SQLite/PostgreSQL and Datastore. Each integration is its own
  deployment, so each can pick its database with `STORAGE_ADAPTER`, or bring its own adapter
  (`package.module:factory`).
- **`EntityMap`** ties Dialpad objects to external records: a caller (`phone:…` / `contact:…`)
  to the CRM contact the integration matched, and each logged activity to the CRM record
  created for it. Matching is the integration's rule (`Connector.match_contact`). The first
  match is stored and reused, and a user's rematch replaces it for future activities only.

## 7. Integration lifecycle

- **Dialpad subscriptions** are created per target (user, department, call center), never per
  company. A reconciler compares what should exist with what does, and is safe to rerun or run
  concurrently. A weekly sweep, staggered across companies, catches drift.
- **Logging configuration** is inherited down the target hierarchy (company → office → user)
  and can be overridden per target. Recomputes run one at a time per company, so an admin's
  change can't be overwritten by a background recompute that read older data.
- **Authentication:** OAuth with PKCE. Admins can connect multiple Dialpad credentials as
  primary and fallback. Token refresh uses compare-and-set so concurrent refreshes don't clobber
  each other. CRM credentials resolve to the user's own first, then the company's.

## 8. Extensibility

- **A connector** is a small interface (auth, the UI manifest, contact search, get, create and
  match, error mapping) plus a contract test suite it must pass. Whatever else its listeners
  need is its own methods. HubSpot's is split into small modules: contacts, calls, messages,
  references, a pure property mapping and the raw CRM client.
- **A listener or feature** is the unit of business logic. It registers event handlers, routes,
  jobs, schedules, required Dialpad scopes, its settings, indexes and the data it owns (deleted
  on uninstall). The service refuses to start if a required scope isn't granted.
- **The UI contact card** is driven by a manifest the connector provides, so the UI needs no
  connector-specific code.

## 9. Security, privacy and developer experience

- **Data protection:** tokens are encrypted at rest (and not indexed); activity records expire
  after their retention period (HubSpot: 90 days by default); uninstall deletes company data,
  including every kind listeners declared, while keeping an audit log.
- **Sessions:** the UI swaps the Dialpad `id_token` for a 15-minute session token kept in memory
  only. It can renew itself for at most `SESSION_MAX_AGE_SECONDS` (8 hours) after the Dialpad
  login. After that the iframe must log in again, so removed users and lost admin rights take
  effect.
- **Input checks:** a user can only rematch caller ids (`phone:` / `contact:`), never the keys of
  logged activities. External data is never rendered as raw HTML, and only messages from
  Dialpad's own pages are trusted.
- **No dev secrets in production:** with the real Dialpad the service refuses to start while
  any secret still has its public dev default.
- **Developer experience:** integrations are packages built on a public SDK
  (`framework_sanic.sdk`). `framework-sanic new` scaffolds one (connector, call/SMS/note
  listeners, tests), `framework-sanic dev` runs everything locally with demo data and fake
  listeners, and `framework-sanic check` is the gate: SDK boundary, plugins, wiring, contract
  suite, tests. Build gates are backed by tests: contract suites (repository, queue, cache,
  connector), property tests, black-box HTTP tests and the load test.

## Tradeoffs and open questions

- 19 assumptions are flagged as unconfirmed and isolated behind config or an interface, such
  as the `id_token` format and the department-call payload shape. See
  [open-questions.md](open-questions.md).
- The Datastore, Pub/Sub and PostgreSQL paths haven't been run against real services: they're
  checked by contract suites against stand-ins, by generated-query checks and by code review.
  Multi-field range queries on Datastore depend on its newer multi-inequality support.
- Paging uses limit and offset. That's fine for UI pages, but deep pages are slow (and billed on
  Datastore); cursor paging would be the next step if needed.
- The storage model trades database-enforced structure (columns, foreign keys, migrations) for
  one port that works on every backend and needs no per-integration migrations. Model changes
  must stay backward compatible: `schema_version` is stored but no migration runner reads it.
- A bug worth mentioning: credentials disappeared once their token expired, because the
  token-expiry field shared its name with the database's record-expiry field. It was renamed to
  `token_expires_at`.
