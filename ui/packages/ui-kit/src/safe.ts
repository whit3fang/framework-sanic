/** Only http(s) links from external data may be rendered; everything else (javascript:, data:) is dropped. */
export function safeHttpUrl(raw: unknown): string | null {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}
