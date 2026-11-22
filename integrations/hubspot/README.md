# HubSpot integration

The reference integration: a `RestConnector` for HubSpot CRM (v3 objects, v4 default
associations) that supports the built-in `call_logging` and `sms_logging` features, plus a
placeholder `hubspot.sms_campaign` feature.

```bash
CONNECTOR=hubspot FEATURES=call_logging,sms_logging framework-sanic serve
```

## Structure

Each module has one job, and each layer only uses the layers below it:

```
connector.py   HubSpotConnector: the framework's ports (Connector, CallLogger, SmsLogger), delegating to:
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
associations), `crm.py`; to support a new framework feature, add a module like `calls.py` and
delegate to it from `connector.py`.

## Settings

| Setting | Default | |
|---|---|---|
| `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` | empty | HubSpot app credentials |
| `HUBSPOT_API_BASE` | `https://api.hubapi.com` | |
| `HUBSPOT_PORTAL_ID` | empty | Fallback portal for record links (normally taken from the OAuth token) |

Production runs it on the framework base image (`[gcp,redis]` extras); the package itself
depends only on plain `framework-sanic`.

Tests: `uv run pytest integrations/hubspot` runs the framework's connector contract suite against
an in-memory HubSpot API (`tests/fake_hubspot_api.py`), HubSpot-specific checks, an end-to-end
run through the framework harness, and unit tests of the pure mapping (`tests/test_hubspot_mapping.py`). It is not yet
verified against a HubSpot sandbox: record one with `framework_sanic.testing.recording`.
