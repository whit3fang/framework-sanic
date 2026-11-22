"""HubSpot SMS campaign feature: specified separately and out of scope for this build.

Kept as a placeholder to show where an integration's own feature lives. Enable with
``FEATURES=hubspot.sms_campaign``; it registers nothing yet.
"""

from __future__ import annotations

from framework_sanic.sdk import FeatureContext


class SmsCampaignFeature:
    name = "hubspot.sms_campaign"

    def setup(self, ctx: FeatureContext) -> None:
        ctx.declare_config("HUBSPOT_CLIENT_ID")
        # Routes, jobs and schedules for the campaign will be registered here.


feature = SmsCampaignFeature()
