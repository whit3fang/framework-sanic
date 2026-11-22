"""SMS logging (used by ``listeners/sms.py``): one HubSpot communication per SMS, associated with the
matched contact."""

from __future__ import annotations

from framework_sanic.sdk import ConnectorCredential, EntityMap

from .activities import LoggedSms
from .crm import CrmClient
from .mapping import sms_properties
from .references import ActivityReferences, logged


class SmsActivities:
    def __init__(self, crm: CrmClient, references: ActivityReferences) -> None:
        self.crm = crm
        self.references = references

    async def log(self, cred: ConnectorCredential, sms: LoggedSms, text: str | None) -> EntityMap:
        await self.references.ensure_property(cred, "communications")
        created = await self.crm.create(cred, "communications", sms_properties(sms, text))
        record = logged(sms.activity_key, "communications", created["id"])
        contact = sms.contact
        if contact:
            await self.crm.associate(
                cred, "communications", record.external_id, "contacts", contact.external_id
            )
        return record
