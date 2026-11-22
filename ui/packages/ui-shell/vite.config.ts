import { fileURLToPath } from "node:url";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// The production build ships inside the Python package (src/framework_sanic/ui_dist), so the
// service serves it at /app/ with no Node at runtime. VERIFY: static hosting vs served by the service.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";
const packagedUi = fileURLToPath(new URL("../../../src/framework_sanic/ui_dist", import.meta.url));

export default defineConfig({
  base: "/app/",
  plugins: [vue()],
  build: { outDir: packagedUi, emptyOutDir: true },
  server: {
    port: Number(process.env.UI_PORT ?? 5173),
    strictPort: true,
    proxy: { "^/(session|contact|notes|oauth|admin|dev)": apiTarget },
  },
  test: { environment: "jsdom" },
});
