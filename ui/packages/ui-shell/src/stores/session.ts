import { createClient, type ApiClient, type SessionOut } from "@ifw/api-client";
import { defineStore } from "pinia";
import { ref, shallowRef } from "vue";
import type { CallContext } from "../bridge/dialpad";

export const useSessionStore = defineStore("session", () => {
  const api: ApiClient = createClient({ baseUrl: import.meta.env.VITE_API_BASE ?? "" });
  // Session state lives in memory only (never localStorage).
  const session = shallowRef<SessionOut | null>(null);
  const error = ref<string | null>(null);
  const call = shallowRef<CallContext | null>(null);
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;

  function scheduleRefresh(s: SessionOut) {
    clearTimeout(refreshTimer);
    const ms = Math.max(30_000, (s.expires_at - Date.now() / 1000 - 60) * 1000);
    refreshTimer = setTimeout(() => void refresh(), ms);
  }

  async function start(idToken: string) {
    try {
      session.value = await api.startSession(idToken);
      scheduleRefresh(session.value);
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    }
  }

  async function refresh() {
    try {
      session.value = await api.refreshSession();
      scheduleRefresh(session.value);
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    }
  }

  return { api, session, error, call, start, refresh };
});
