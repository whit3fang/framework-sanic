"""SMS logging (``sms_logging`` feature): one HubSpot communication per SMS, associated with the
matched contact."""

from __future__ import annotations

from framework_sanic.sdk import ConnectorCredential, EntityMap
from framework_sanic.sdk.sms_logging import SmsLog

from .crm import CrmClient
from .mapping import sms_properties
from .references import ActivityReferences, logged


class SmsActivities:
    def __init__(self, crm: CrmClient, references: ActivityReferences) -> None:
        self.crm = crm
        self.references = references

    async def log(self, cred: ConnectorCredential, sms: SmsLog) -> EntityMap:
        await self.references.ensure_property(cred, "communications")
        created = await self.crm.create(cred, "communications", sms_properties(sms))
        record = logged(sms.activity.activity_key, "communications", created["id"])
        contact = sms.activity.contact
        if contact:
            await self.crm.associate(
                cred, "communications", record.external_id, "contacts", contact.external_id
            )
        return record
