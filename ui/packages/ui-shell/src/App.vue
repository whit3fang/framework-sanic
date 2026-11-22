<script setup lang="ts">
// Shows the matched external contact for the current call, rematch and notes.
// No call/SMS history is shown (non-goal).
import { ContactCard, NoticeBanner } from "@ifw/ui-kit";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, onMounted, onUnmounted, ref } from "vue";
import { allowedOrigins, dialpadRef, listenForCallContext, readDevCallContext, readIdToken } from "./bridge/dialpad";
import NotesPanel from "./components/NotesPanel.vue";
import RematchDialog from "./components/RematchDialog.vue";
import { useSessionStore } from "./stores/session";

const store = useSessionStore();
const queryClient = useQueryClient();
const rematching = ref(false);
let stopListening: (() => void) | undefined;

const ref_ = computed(() => (store.call ? dialpadRef(store.call) : null));
const ready = computed(() => store.session?.state === "ready");
// Notes belong to the call_logging feature; the panel is shown only when it is enabled.
const notesEnabled = computed(() => store.session?.features.includes("call_logging") ?? false);

const contact = useQuery({
  queryKey: computed(() => ["contact", ref_.value]),
  queryFn: () => store.api.getContact(ref_.value as string, store.call?.phoneNumber ?? undefined),
  enabled: computed(() => ready.value && !!ref_.value),
  staleTime: 30_000,
});

async function connectExternal(scope: "user" | "company") {
  const { authorize_url } = await store.api.externalConnectUrl(scope);
  const popup = window.open(authorize_url, "connect", "width=600,height=700");
  const timer = setInterval(() => {
    if (!popup || popup.closed) {
      clearInterval(timer);
      void store.refresh();
    }
  }, 500);
}

function connectDialpad() {
  if (!store.session) return;
  const popup = window.open(store.session.dialpad_connect_url, "connect", "width=600,height=700");
  const timer = setInterval(() => {
    if (!popup || popup.closed) {
      clearInterval(timer);
      void store.refresh();
    }
  }, 500);
}

function onRematched() {
  rematching.value = false;
  void queryClient.invalidateQueries({ queryKey: ["contact"] });
}

onMounted(() => {
  const token = readIdToken();
  if (token) void store.start(token);
  else store.error = "Open this app from Dialpad.";
  const devCall = readDevCallContext(import.meta.env.DEV);
  if (devCall) store.call = devCall;
  stopListening = listenForCallContext(allowedOrigins(import.meta.env.VITE_DIALPAD_ORIGINS), (ctx) => {
    store.call = ctx;
  });
});
onUnmounted(() => stopListening?.());
</script>

<template>
  <main>
    <NoticeBanner v-if="store.error" tone="warning" :message="store.error" />
    <template v-else-if="store.session">
      <NoticeBanner v-if="store.session.admin_notice" :message="store.session.admin_notice" />

      <section v-if="store.session.state === 'needs_admin_connect'">
        <p>Connect your Dialpad company to start logging calls and messages.</p>
        <button type="button" @click="connectDialpad">Connect Dialpad</button>
      </section>
      <NoticeBanner v-else-if="store.session.state === 'awaiting_admin'" message="Waiting for a Dialpad admin to connect this integration." />
      <section v-else-if="store.session.state === 'needs_external_connect'">
        <p>Connect your {{ store.session.manifest.display_name }} account.</p>
        <button type="button" @click="connectExternal('user')">Connect my account</button>
        <button v-if="store.session.is_admin" type="button" @click="connectExternal('company')">Connect for the company</button>
      </section>

      <template v-else-if="ready">
        <p v-if="!store.call">Waiting for a call…</p>
        <template v-else-if="ref_">
          <p v-if="contact.isLoading.value">Looking up contact…</p>
          <NoticeBanner v-else-if="contact.error.value" tone="warning" :message="String(contact.error.value)" />
          <RematchDialog
            v-else-if="rematching && contact.data.value"
            :api="store.api"
            :dialpad-ref="ref_"
            :manifest="contact.data.value.manifest"
            @done="onRematched"
            @cancel="rematching = false"
          />
          <ContactCard
            v-else-if="contact.data.value"
            :entity="contact.data.value.entity"
            :manifest="contact.data.value.manifest"
            @rematch="rematching = true"
          />
          <NotesPanel v-if="notesEnabled" :api="store.api" :call-id="store.call.callId" :dialpad-ref="ref_" />
        </template>
      </template>
    </template>
    <p v-else>Loading…</p>
  </main>
</template>
