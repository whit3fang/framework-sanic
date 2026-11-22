"""What the Dialpad contact card shows for a HubSpot contact (no frontend code needed)."""

from __future__ import annotations

from framework_sanic.sdk import ManifestField, UiManifest

MANIFEST = UiManifest(
    connector="hubspot",
    display_name="HubSpot",
    fields=[
        ManifestField(key="name", label="Name"),
        ManifestField(key="email", label="Email", kind="email"),
        ManifestField(key="phone", label="Phone", kind="phone"),
        ManifestField(key="company", label="Company"),
        ManifestField(key="jobtitle", label="Job title"),
    ],
    link_out_template=None,  # each entity carries its portal-specific url
    create_fields=[
        ManifestField(key="firstname", label="First name"),
        ManifestField(key="lastname", label="Last name"),
        ManifestField(key="email", label="Email", kind="email"),
        ManifestField(key="phone", label="Phone", kind="phone"),
    ],
)
