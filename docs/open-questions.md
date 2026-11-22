# Open questions (VERIFY)

Every unconfirmed assumption is isolated behind a port or a config value and marked `# VERIFY`
in code. Resolve each item, then update the adapter/config named below. Nothing else should need
to change.

| # | Assumption | Where it is isolated | Current behaviour |
|---|---|---|---|
| 1 | `is_admin` exists on the Dialpad user object (and its semantics) | `PlatformIdentity.is_admin`, `User.is_admin`, `adapters/dialpad/platform.py::identify` | `is_admin or is_super_admin` from `/users/me`; non-admins cannot complete admin connect |
| 2 | `id_token` format and claims | `IdTokenVerifier` port, `adapters/dialpad/id_token.py` | HS256 JWT signed with the OAuth client secret; claims `user_id`/`sub`, `company_id`/`cid`, optional `is_admin`; falls back to the stored `User.is_admin` when the claim is absent |
| 3 | Department-leg `operator` JSON shape | `CallEvent.operator` (kept raw) + `CallEvent.operator_user_id()` | Tries `id`, `user_id`, `target.id`, `operator.id`; owner is `None` if none match. Fixture: `tests/integration/test_stage1.py::test_operator_leg_dropped_when_group_logging_enabled` |
| 4 | `entry_point_target` semantics (identifying operator legs) | `CallEvent.is_operator_leg` | A leg is an operator leg when `target.type == user` and `entry_point_target.type in {department, callcenter}` |
| 5 | Default call-state list | `DIALPAD_CALL_STATES` | `hangup,missed,voicemail,recording` |
| 6 | Whether group SMS (`contact_group`) should be logged | `SmsEvent.contact_group`, SMS `SubscriptionSpec` | Logged like any other SMS; `contact_group` is parsed but unused |
| 7 | Default behaviour for unknown callers | HubSpot: `HUBSPOT_CALLS_UNKNOWN_CALLER` / `HUBSPOT_SMS_UNKNOWN_CALLER` (`log_unassociated` / `create_contact` / `skip`); other integrations decide in their listeners | `log_unassociated`: the activity is logged without a contact association |
| 8 | HubSpot object used for SMS activity | `integrations/hubspot/src/framework_sanic_hubspot/mapping.py::sms_properties` | CRM `communications` object with `hs_communication_channel_type=SMS` |
| 9 | HubSpot OAuth scopes / PKCE support | `HubSpotAuth.scopes`, `HubSpotAuth.authorize_url` | `oauth crm.objects.contacts.read crm.objects.contacts.write crm.schemas.calls.write`; PKCE challenge sent (ignored if unsupported) |
| 10 | Change-log API shape and scopes | `ChangeLogSource` port, `adapters/changelog/noop.py` | No-op adapter. Lifecycle actions configured by `CHANGELOG_LIFECYCLE_ACTIONS`; matching events enqueue a reconcile |
| 11 | Data residency requirements | Storage/queue/KMS adapters are per-deployment config | Single region assumed |
| 12 | Datastore client choice under load | `adapters/storage/datastore/repository.py` | Official sync client wrapped in `asyncio.to_thread`. **Benchmark** against an async client at target load (30M calls/day) |
| 13 | Real 1,200 req/min enforcement (burst vs sustained) | `DIALPAD_RATE_LIMIT_PER_MINUTE`, `RateLimiter` | Token bucket with capacity = 1 minute of budget (allows a full-minute burst); UI lane is never held back by the reserve |
| 14 | Dialpad list endpoints for departments / call centers | `adapters/dialpad/platform.py::ENDPOINTS` | `/api/v2/departments`, `/api/v2/callcenters`; a 4xx there is treated as "none" |
| 15 | Webhook payload format | `adapters/dialpad/webhook.py` | HS256 JWT body signed with the endpoint secret. Per-company secrets are derived as HMAC(`WEBHOOK_SECRET_KEY`, company id) so verification needs no DB/Redis |
| 16 | Static UI hosting vs served by the service | `SERVE_UI_DIR`, `ui/packages/ui-shell/vite.config.ts` (`base: /app/`) | Either works: set `SERVE_UI_DIR` to the Vite `dist/` to serve it from the service |
| 17 | Dialpad → iframe `postMessage` call-context shape | `ui-shell/src/bridge/dialpad.ts::parseCallContext` | Accepts `{type: call_context, payload: {call_id, phone_number, contact_id}}` and flat variants |
| 18 | Scale numbers (3,000 companies × 1,000 users; ≥30M calls/day; ≤30M SMS/day) | `SHARD_COUNT`, `WORKER_CONCURRENCY`, `PER_COMPANY_CONCURRENCY` | Validate against real usage before sizing |
| 19 | Dialpad uninstall signal | `POST /admin/uninstall` | Admin-triggered uninstall deletes all company data (audit log kept). No Dialpad-initiated uninstall webhook is wired up |
