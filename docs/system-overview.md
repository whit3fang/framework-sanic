# System overview

A high-level walkthrough of the integration framework: what it solves, how it is shaped, and
where the hard parts are. It is written to be talked through in about 15 minutes. Details live
in the code and the [README](../README.md), and unconfirmed assumptions are listed in
[open-questions.md](open-questions.md).

Suggested emphasis: spend most of the time on sections 3–5. That's where the hard
distributed-systems reasoning is. Keep the others to about a minute each.

## 1. The problem and the constraints

Dialpad integrations with external systems like CRMs all need the same foundation: connect
accounts, subscribe to call and SMS events, receive them reliably, and show the matched contact
inside an embedded panel. What each integration then *does* with an event varies. So the
framework owns receiving and delivering events, and each integration plugs in its own processing.
HubSpot call logging and SMS logging, two independent features, are the first integration built on it.

The constraints that shape every decision:

- **Scale:** about 3,000 companies, roughly 30M calls a day.
- **Dialpad's webhook rules:** a 5-second timeout, and no retry on 4xx responses.
- **Privacy:** SMS text is never stored and transcripts are dropped.

## 2. Architecture: ports and adapters

- The core is plain Python. Everything external (storage, queue, cache, KMS, Dialpad, the CRM)
  sits behind an interface, and config picks the implementation.
- The web framework is isolated to one file (`http/adapters/sanic.py`), and a lint test
  enforces that. Moving from Sanic to FastAPI means adding one adapter and running the same
  black-box HTTP tests against it.
- **Why:** each adapter can be tested on its own, and new connectors and features can be added
  without touching the core.

## 3. The event pipeline: framework receives, features process

This is the heart of the system, and the most important boundary in it.

```
            ┌──────────────────────── framework ────────────────────────┐   ┌─ feature ─┐
Dialpad ──► │ verify ─► encrypt SMS text ─► fan out ─► queue ─► worker ─│─► │ on_event  │
webhook     └───────────────────────────────────────────────────────────┘   └───────────┘
```

**What the framework guarantees:**

- **Buffer-first accept path:** verify the signature, encrypt the SMS text, put the event on a
  durable queue, return 2xx. It keeps working when the database and Redis are down.
- **Fan-out:** one queue message per feature that registered a handler for that event kind, so
  each feature retries independently. A failing feature never makes another see an event twice.
- **Delivery:** at least once, through `ctx.on_event("call", handler)`. The handler's result
  (acknowledge, retry later, deadletter) drives the queue.
- **Subscriptions follow handlers:** Dialpad subscriptions are created only for event kinds some
  enabled feature handles. With no such feature, nothing is subscribed.
- **A toolkit for processing:** atomic repository upserts, a transactional outbox, and
  `SyncGuards` (parking, credentials, rate budgets, leases, error mapping).

**What a feature decides:** which events it wants, what they mean, what it writes to the
external system, and when.

**Reference features** (opt-in and independent, both used by HubSpot):

- **`call_logging`:**
  - **Stage 1:** a cheap merge of call events into an activity record. It never calls external
    services.
  - **Outbox:** the activity change and the sync job are written in the same transaction, so a
    crash can't lose a sync.
  - **Stage 2:** sync to the CRM through the `CallLogger` connector port, using the framework's
    guards, then attach recordings and notes.
- **`sms_logging`:** insert each SMS once, then a sync job decrypts the text just long enough to
  send it through the `SmsLogger` port. The text is never stored.

## 4. Correctness under messy delivery

The framework's contract is at-least-once, so handlers must be idempotent. `call_logging` shows
how:

- Dialpad retries arrive out of order and duplicated. The call record is an order-independent
  fold: the highest-ranked event wins each field, and a terminal state like hangup can never be
  undone by a late event.
- Property-based tests shuffle, duplicate and drop events, and the final record must come out
  identical.
- **Idempotency everywhere:** deterministic keys, insert-once SMS, client-generated note IDs,
  and a reference stored on each CRM record so a retried sync never creates a duplicate.
- Redis is only a fast path. Correctness comes from atomic database upserts.

## 5. Multi-tenancy, fairness and scale

These are framework guarantees, so every feature gets them for free:

- Queues are sharded by company. Each company has its own token bucket for API calls and a cap
  on how many of its jobs run at once.
- Priority lanes: UI requests first, then live events, then background sweeps.
- A company with a broken connection is "parked": its external sync waits without failing,
  while event intake keeps flowing.
- The load test shows quiet companies finishing in milliseconds while a noisy company's backlog
  takes seconds.

## 6. Integration lifecycle

- **Dialpad subscriptions** are created per target (user, department, call center), never per
  company. A reconciler compares what should exist with what does, and is safe to rerun or run
  concurrently. A weekly sweep, staggered across companies, catches drift.
- **Authentication:** OAuth with PKCE. Admins can connect multiple Dialpad credentials as
  primary and fallback. Token refresh uses compare-and-set so concurrent refreshes don't clobber
  each other. CRM credentials resolve to the user's own first, then the company's.

## 7. Extensibility

- A connector is one interface to implement plus a contract test suite it must pass.
- A feature is the unit of business logic. It registers event handlers, routes, jobs,
  schedules, required permission scopes and the data it owns (deleted on uninstall). The
  service refuses to start if a feature needs a scope that isn't granted, or if a logging feature
  is enabled with a connector that doesn't implement its port.
- The base connector is deliberately small (auth, contact lookup, error mapping). Features
  define the extra ports they need, like `call_logging`'s `CallLogger` and `sms_logging`'s `SmsLogger`.
- The UI contact card is driven by a manifest the connector provides, so the UI needs no
  connector-specific code.

## 8. Security, privacy and developer experience

- **Data protection:** tokens are encrypted at rest; call and SMS activity records expire
  after 90 days; uninstall deletes company data, including every kind features declared, while
  keeping an audit log.
- **UI:** the session token is kept in memory only, external data is never rendered as raw
  HTML, and only messages from Dialpad's own pages are trusted.
- **Developer experience:** `scripts/dev.sh` starts the server, the UI and SQLite with demo
  data. Build gates are backed by tests: contract suites, property tests, black-box HTTP tests
  and the load test.

## Tradeoffs and open questions

- About 19 assumptions are flagged as unconfirmed and isolated behind config or an interface,
  such as the `id_token` format and the department-call payload shape. See
  [open-questions.md](open-questions.md).
- The Datastore and Pub/Sub adapters haven't been run against real services, and there's an
  open choice between the current thread-pool wrapper and an async Datastore client under load.
- A bug worth mentioning: credentials disappeared once their token expired, because the
  token-expiry field shared its name with the database's record-expiry field. It was renamed to
  `token_expires_at`.
