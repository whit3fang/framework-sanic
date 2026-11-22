<script setup lang="ts">
// Notes use client-generated ids, so retrying a failed submit can never create a duplicate.
import { newNoteId, type ApiClient } from "@ifw/api-client";
import { ref } from "vue";

const props = defineProps<{ api: ApiClient; callId: string; dialpadRef?: string | null }>();

const body = ref("");
const pendingId = ref<string | null>(null);
const status = ref<"idle" | "saving" | "saved" | "error">("idle");
const error = ref<string | null>(null);

async function submit() {
  const text = body.value.trim();
  if (!text) return;
  pendingId.value ??= newNoteId(); // reused on retry
  status.value = "saving";
  try {
    await props.api.addNote(pendingId.value, props.callId, text, props.dialpadRef ?? undefined);
    status.value = "saved";
    body.value = "";
    pendingId.value = null;
  } catch (e) {
    status.value = "error";
    error.value = e instanceof Error ? e.message : String(e);
  }
}
</script>

<template>
  <form class="notes" @submit.prevent="submit">
    <label for="note">Add a note to this call</label>
    <textarea id="note" v-model="body" maxlength="10000" rows="3" data-test="note-body" />
    <button type="submit" :disabled="status === 'saving'" data-test="note-submit">
      {{ status === "error" ? "Retry" : "Save note" }}
    </button>
    <span v-if="status === 'saved'" data-test="note-saved">Saved</span>
    <span v-if="status === 'error'" class="error">{{ error }}</span>
  </form>
</template>
