/**
 * Dialpad iframe bridge.
 *
 * - The iframe URL carries the per-user `id_token` (format VERIFY). It is read once and removed
 *   from the address bar so it does not linger in history.
 * - Call context (number, call id) arrives via postMessage. `event.origin` is validated against
 *   the configured Dialpad origins; anything else is ignored.
 *   VERIFY: the exact message shape; parsed defensively here.
 */

export interface CallContext {
  callId: string;
  phoneNumber: string | null;
  contactId: string | null;
}

export function readIdToken(loc: Location = window.location, hist: History = window.history): string | null {
  const url = new URL(loc.href);
  const token = url.searchParams.get("id_token");
  if (token) {
    url.searchParams.delete("id_token");
    hist.replaceState(hist.state, "", url.toString());
  }
  return token;
}

export function allowedOrigins(raw: string | undefined): string[] {
  return (raw ?? "https://dialpad.com,https://*.dialpad.com")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function originAllowed(origin: string, patterns: string[]): boolean {
  return patterns.some((p) => {
    if (!p.includes("*")) return origin === p;
    const re = new RegExp("^" + p.split("*").map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[a-z0-9-]+") + "$");
    return re.test(origin);
  });
}

export function parseCallContext(data: unknown): CallContext | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  const payload = (typeof d.payload === "object" && d.payload ? d.payload : d) as Record<string, unknown>;
  const type = d.type ?? d.event;
  if (type !== undefined && !["call_context", "call_ringing", "call_connected", "call"].includes(String(type))) {
    return null;
  }
  const callId = payload.call_id ?? payload.callId;
  if (callId === undefined || callId === null) return null;
  const phone = payload.phone_number ?? payload.external_number ?? payload.number;
  const contact = payload.contact_id ?? payload.contactId;
  return {
    callId: String(callId),
    phoneNumber: typeof phone === "string" ? phone : null,
    contactId: contact === undefined || contact === null ? null : String(contact),
  };
}

/** Dialpad contact reference, matching the backend's `contact_ref_from_party`. */
export function dialpadRef(ctx: CallContext): string | null {
  if (ctx.contactId) return `contact:${ctx.contactId}`;
  if (ctx.phoneNumber) return `phone:${ctx.phoneNumber}`;
  return null;
}

export function listenForCallContext(
  patterns: string[],
  onContext: (ctx: CallContext) => void,
  target: Window = window,
): () => void {
  const handler = (event: MessageEvent) => {
    if (!originAllowed(event.origin, patterns)) return;
    const ctx = parseCallContext(event.data);
    if (ctx) onContext(ctx);
  };
  target.addEventListener("message", handler);
  return () => target.removeEventListener("message", handler);
}

/**
 * Local development only: outside Dialpad nothing posts the call context, so `scripts/dev.sh`
 * puts it in the URL (`?dev_call_id=…&dev_phone=…`). Ignored in production builds.
 */
export function readDevCallContext(isDev: boolean, loc: Location = window.location): CallContext | null {
  if (!isDev) return null;
  const params = new URL(loc.href).searchParams;
  const callId = params.get("dev_call_id");
  if (!callId) return null;
  return { callId, phoneNumber: params.get("dev_phone"), contactId: params.get("dev_contact_id") };
}
