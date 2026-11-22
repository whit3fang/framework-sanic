/**
 * Optional HubSpot-specific UI extensions. The contact card itself is manifest-driven and needs
 * nothing from here; this package is the place for connector-only extras (e.g. branding).
 */
export const hubspotUi = {
  connector: "hubspot",
  accentColor: "#ff7a59",
} as const;
