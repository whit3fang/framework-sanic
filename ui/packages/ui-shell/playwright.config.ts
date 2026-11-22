import { defineConfig } from "@playwright/test";

// End-to-end test inside the fake Dialpad harness (docker compose up, then `pnpm e2e`).
export default defineConfig({
  testDir: "e2e",
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173" },
});
