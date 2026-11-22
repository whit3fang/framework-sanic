/**
 * Typed API client. Types in ./generated.ts are produced from the Pydantic models
 * (`pnpm generate:api`), so request/response shapes never drift from the backend.
 *
 * The session token is held in module memory only: never localStorage/sessionStorage.
 */
import type {
  AuthorizeOut,
  ContactOut,
  Entity,
  NoteOut,
  RematchOut,
  SearchOut,
  SessionOut,
} from "./generated";

export type * from "./generated";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

let sessionToken: string | null = null;
let idToken: string | null = null;

export interface ClientOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
}

export function createClient({ baseUrl, fetchImpl = fetch }: ClientOptions) {
  const base = baseUrl.replace(/\/$/, "");

  async function request<T>(method: string, path: string, opts: { query?: Record<string, string>; body?: unknown } = {}): Promise<T> {
    const url = new URL(base + path, window.location.origin);
    for (const [k, v] of Object.entries(opts.query ?? {})) url.searchParams.set(k, v);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
    else if (idToken) headers["X-Dialpad-Id-Token"] = idToken;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetchImpl(url.toString(), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "omit",
    });
    const text = await resp.text();
    const data = text ? JSON.parse(text) : null;
    if (!resp.ok) {
      const err = data?.error ?? {};
      throw new ApiError(resp.status, err.code ?? "error", err.message ?? resp.statusText);
    }
    return data as T;
  }

  return {
    /** Exchange the Dialpad id_token for an in-memory session token. */
    async startSession(token: string): Promise<SessionOut> {
      idToken = token;
      sessionToken = null;
      const session = await request<SessionOut>("GET", "/session");
      sessionToken = session.session_token;
      idToken = null; // only the short-lived session token is kept, in memory
      return session;
    },
    refreshSession: () =>
      request<SessionOut>("GET", "/session").then((s) => {
        sessionToken = s.session_token;
        return s;
      }),
    logout: () => request("POST", "/session/logout").finally(() => (sessionToken = null)),
    getContact: (dialpadRef: string, phone?: string) =>
      request<ContactOut>("GET", "/contact", { query: { dialpad_ref: dialpadRef, ...(phone ? { phone } : {}) } }),
    searchContacts: (q: string) => request<SearchOut>("GET", "/contact/search", { query: { q } }),
    rematch: (dialpadRef: string, entity: Pick<Entity, "external_id" | "external_type">) =>
      request<RematchOut>("POST", "/contact/rematch", {
        body: {
          dialpad_ref: dialpadRef,
          external_id: entity.external_id,
          external_type: entity.external_type,
          confirm_forward_only: true,
        },
      }),
    createContact: (dialpadRef: string, properties: Record<string, string>) =>
      request<Entity>("POST", "/contact", { body: { dialpad_ref: dialpadRef, properties } }),
    addNote: (noteId: string, callId: string, body: string, dialpadRef?: string) =>
      request<NoteOut>("POST", "/notes", { body: { note_id: noteId, call_id: callId, body, dialpad_ref: dialpadRef } }),
    externalConnectUrl: (scope: "user" | "company") =>
      request<AuthorizeOut>("GET", "/oauth/external/start", { query: { scope } }),
  };
}

export type ApiClient = ReturnType<typeof createClient>;

/** Client-generated ids make note creation idempotent across retries. */
export function newNoteId(): string {
  return `note-${crypto.randomUUID().replace(/-/g, "")}`;
}

/** Test helper. */
export function _resetTokens(): void {
  sessionToken = null;
  idToken = null;
}
