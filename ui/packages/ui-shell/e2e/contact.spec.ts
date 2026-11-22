import { expect, test } from "@playwright/test";

// Runs against the fake Dialpad harness: `docker compose up`, `pnpm dev`, then `pnpm e2e`.
// E2E_ID_TOKEN must be signed with the harness DIALPAD_CLIENT_SECRET (see tools/fake_dialpad.py).
test("contact card, rematch confirmation and note", async ({ page }) => {
  const token = process.env.E2E_ID_TOKEN;
  test.skip(!token, "E2E_ID_TOKEN not set");
  await page.goto(`/app/?id_token=${token}`);
  await expect(page).not.toHaveURL(/id_token=/);
  await page.evaluate(() =>
    window.dispatchEvent(new MessageEvent("message", {
      origin: "https://dialpad.com",
      data: { type: "call_context", payload: { call_id: "4242", phone_number: "+14155550100" } },
    })),
  );
  await expect(page.getByText("Ada Lovelace")).toBeVisible();
  await page.getByTestId("rematch").click();
  await page.getByTestId("search").fill("Ada");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("radio").first().check();
  await expect(page.getByTestId("forward-only")).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByTestId("note-body").fill("call back Tuesday");
  await page.getByTestId("note-submit").click();
  await expect(page.getByTestId("note-saved")).toBeVisible();
});
