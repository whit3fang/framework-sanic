<script setup lang="ts">
/**
 * Manifest-driven contact card: the connector's ui_manifest() lists fields, labels and link-outs;
 * this component has no connector-specific code. All external data is rendered through text
 * interpolation (escaped); v-html is never used.
 */
import type { Entity, UiManifest } from "@ifw/api-client";
import { computed } from "vue";
import FieldValue from "./FieldValue.vue";
import { safeHttpUrl } from "./safe";

const props = defineProps<{ entity: Entity | null; manifest: UiManifest; linked?: boolean }>();
defineEmits<{ rematch: []; create: [] }>();

const link = computed(() => {
  if (!props.entity) return null;
  if (props.entity.url) return safeHttpUrl(props.entity.url);
  const tpl = props.manifest.link_out_template;
  return tpl ? safeHttpUrl(tpl.replace("{id}", encodeURIComponent(props.entity.ref.id))) : null;
});

const rows = computed(() =>
  props.manifest.fields
    .map((f) => ({ field: f, value: props.entity?.properties?.[f.key] }))
    .filter((r) => r.value !== undefined && r.value !== null && r.value !== ""),
);
</script>

<template>
  <section class="contact-card" aria-live="polite">
    <header>
      <h2>{{ manifest.object_label }}</h2>
      <span v-if="linked" class="badge" title="Matched manually">linked</span>
    </header>
    <template v-if="entity">
      <dl>
        <template v-for="row in rows" :key="row.field.key">
          <dt>{{ row.field.label }}</dt>
          <dd><FieldValue :kind="row.field.kind ?? 'text'" :value="row.value" /></dd>
        </template>
      </dl>
      <a v-if="link" :href="link" target="_blank" rel="noopener noreferrer" data-test="link-out">
        Open in {{ manifest.display_name }}
      </a>
    </template>
    <p v-else data-test="no-match">No matching {{ manifest.object_label.toLowerCase() }} found.</p>
    <footer>
      <button type="button" data-test="rematch" @click="$emit('rematch')">Change match</button>
      <button v-if="!entity && manifest.create_fields?.length" type="button" data-test="create" @click="$emit('create')">
        Create {{ manifest.object_label.toLowerCase() }}
      </button>
    </footer>
  </section>
</template>

<style scoped>
.contact-card { font: 14px/1.4 system-ui, sans-serif; padding: 12px; }
header { display: flex; align-items: center; gap: 8px; }
h2 { font-size: 15px; margin: 0; }
.badge { font-size: 11px; background: #eef; border-radius: 4px; padding: 0 6px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; }
dt { color: #666; }
dd { margin: 0; overflow-wrap: anywhere; }
footer { display: flex; gap: 8px; margin-top: 8px; }
</style>
