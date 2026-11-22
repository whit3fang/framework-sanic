<script setup lang="ts">
// Rematch: search box + explicit confirmation of forward-only behaviour.
import type { ApiClient, Entity, UiManifest } from "@ifw/api-client";
import { ref } from "vue";

const props = defineProps<{ api: ApiClient; dialpadRef: string; manifest: UiManifest }>();
const emit = defineEmits<{ done: [entity: Entity]; cancel: [] }>();

const query = ref("");
const results = ref<Entity[]>([]);
const selected = ref<Entity | null>(null);
const busy = ref(false);
const error = ref<string | null>(null);

function label(e: Entity): string {
  const p = e.properties ?? {};
  return String(p.name ?? p.email ?? p.phone ?? e.external_id);
}

async function search() {
  if (query.value.trim().length < 2) return;
  busy.value = true;
  error.value = null;
  try {
    results.value = (await props.api.searchContacts(query.value.trim())).results;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function confirm() {
  if (!selected.value) return;
  busy.value = true;
  try {
    await props.api.rematch(props.dialpadRef, selected.value);
    emit("done", selected.value);
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="rematch" role="dialog" aria-label="Change contact match">
    <form @submit.prevent="search">
      <input v-model="query" :placeholder="manifest.search_placeholder" data-test="search" />
      <button type="submit" :disabled="busy">Search</button>
    </form>
    <ul>
      <li v-for="e in results" :key="`${e.external_type}:${e.external_id}`">
        <label>
          <input type="radio" name="match" :value="e.external_id" @change="selected = e" />
          {{ label(e) }}
        </label>
      </li>
    </ul>
    <template v-if="selected">
      <p class="forward-only" data-test="forward-only">
        This match applies to future calls and messages only. Activities already logged keep their current contact.
      </p>
      <button type="button" data-test="confirm" :disabled="busy" @click="confirm">Confirm match to {{ label(selected) }}</button>
    </template>
    <button type="button" @click="emit('cancel')">Cancel</button>
    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>
