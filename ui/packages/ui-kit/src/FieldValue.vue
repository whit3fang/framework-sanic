<script setup lang="ts">
// Renders one external value as text or a safe link. No v-html.
import { computed } from "vue";
import { safeHttpUrl } from "./safe";

const props = defineProps<{ kind: string; value: unknown }>();
const text = computed(() => String(props.value ?? ""));
const href = computed(() => {
  if (props.kind === "email") return /^[^\s@]+@[^\s@]+$/.test(text.value) ? `mailto:${text.value}` : null;
  if (props.kind === "phone") return /^\+?[\d\s().-]{3,}$/.test(text.value) ? `tel:${text.value.replace(/[^\d+]/g, "")}` : null;
  if (props.kind === "url") return safeHttpUrl(text.value);
  return null;
});
</script>

<template>
  <a v-if="href" :href="href" rel="noopener noreferrer">{{ text }}</a>
  <span v-else>{{ text }}</span>
</template>
