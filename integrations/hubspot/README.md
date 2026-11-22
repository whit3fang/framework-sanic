# HubSpot integration

The reference integration: a `RestConnector` for HubSpot CRM (v3 objects, v4 default
associations) and the listeners that decide what Dialpad calls, SMS and notes become in HubSpot,
plus a placeholder `hubspot.sms_campaign` feature.

```bash
CONNECTOR=hubspot FEATURES=hubspot.calls,hubspot.sms,hubspot.notes framework-sanic serve
```

## Structure

Each module has one job, and each layer only uses the layers below it:

```
listeners/       what happens to Dialpad activity (enable each with FEATURES=):
  calls.py         hubspot.calls: fold call events into a LoggedCall (+ outbox), then hubspot.sync_call
  fold.py          the order-independent call fold (pure; property tested)
  sms.py           hubspot.sms: store SMS metadata once, then hubspot.sync_sms sends the decrypted text
  notes.py         hubspot.notes: POST /notes -> hubspot.sync_note, which waits for the call to be logged
  settings.py      HUBSPOT_CALLS_* / HUBSPOT_SMS_* settings
activities.py    LoggedCall / LoggedSms (framework models + sync bookkeeping), CallNote
connector.py   HubSpotConnector: the framework's Connector port plus the methods the listeners use, delegating to:
  contacts.py    caller -> contact matching (HubSpot's rule), contact search / get / create
  calls.py       call logging: one HubSpot call per Dialpad call, recording link, notes
  messages.py    SMS logging: one HubSpot communication per SMS
  references.py  idempotency: the dialpad_activity_key property and lookups by it
  mapping.py     pure Dialpad -> HubSpot property mapping (no I/O; unit tested directly)
  crm.py         the HubSpot CRM API: v3 objects/properties, v4 associations (HTTP only, no Dialpad concepts)
auth.py          OAuth endpoints, scopes, portal identification
manifest.py      what the contact card shows
settings.py      HUBSPOT_* settings
features/        HubSpot-only features (sms_campaign placeholder)
```

To change *what* is written to HubSpot, edit `mapping.py`; to change *how* (endpoints,
associations), `crm.py`; to change *when* (filters, retries, unknown callers), the listeners.

## Settings

| Setting | Default | |
|---|---|---|
| `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` | empty | HubSpot app credentials |
| `HUBSPOT_API_BASE` | `https://api.hubapi.com` | |
| `HUBSPOT_PORTAL_ID` | empty | Fallback portal for record links (normally taken from the OAuth token) |
| `HUBSPOT_CALLS_DIRECTIONS`, `HUBSPOT_CALLS_SKIP_STATES` | `inbound,outbound`, empty | which calls are logged (e.g. skip `missed,voicemail`) |
| `HUBSPOT_CALLS_MIN_DURATION_SECONDS`, `HUBSPOT_CALLS_RECORDINGS` | `0`, `true` | skip short completed calls; attach recording links |
| `HUBSPOT_CALLS_UNKNOWN_CALLER`, `HUBSPOT_SMS_UNKNOWN_CALLER` | `log_unassociated` | or `create_contact` / `skip` (VERIFY default) |
| `HUBSPOT_SMS_DIRECTIONS`, `HUBSPOT_SMS_INCLUDE_TEXT` | `inbound,outbound`, `true` | which SMS are logged; `false` logs them without the text (never decrypted) |
| `HUBSPOT_CALLS_RETENTION_DAYS`, `HUBSPOT_SMS_RETENTION_DAYS` | `90` | how long local activity records are kept |

Production runs it on the framework base image (`[gcp,redis]` extras); the package itself
depends only on plain `framework-sanic`.

Tests: `uv run pytest integrations/hubspot` runs the framework's connector contract suite against
an in-memory HubSpot API (`tests/fake_hubspot_api.py`), HubSpot-specific checks, the listeners in the harness (`tests/test_hubspot_listeners.py`), the
call fold (`tests/test_hubspot_fold.py`), an end-to-end
run through the framework harness, and unit tests of the pure mapping (`tests/test_hubspot_mapping.py`). It is not yet
verified against a HubSpot sandbox: record one with `framework_sanic.testing.recording`.
