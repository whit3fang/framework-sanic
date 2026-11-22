<script setup lang="ts">
defineProps<{ message: string; tone?: "info" | "warning" }>();
</script>

<template>
  <div class="notice" :class="tone ?? 'info'" role="status">{{ message }}</div>
</template>

<style scoped>
.notice { padding: 8px 12px; font: 13px system-ui, sans-serif; border-radius: 4px; }
.info { background: #eef4ff; }
.warning { background: #fff4e5; }
</style>
