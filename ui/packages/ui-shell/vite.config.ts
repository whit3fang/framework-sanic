import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// VERIFY: static hosting vs served by the service. `base: "/app/"` matches SERVE_UI_DIR serving.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  base: "/app/",
  plugins: [vue()],
  server: {
    port: Number(process.env.UI_PORT ?? 5173),
    strictPort: true,
    proxy: { "^/(session|contact|notes|oauth|admin|dev)": apiTarget },
  },
  test: { environment: "jsdom" },
});
