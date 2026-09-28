import { expect, test } from "@playwright/test";

/**
 * Optional smoke against a running simulation desk.
 * Start the stack first (docker compose up) with APP_MODE=simulation.
 */
test.describe("Repair Desk dashboard smoke", () => {
  test("loads shell with simulation mode and core landmarks", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Devin Repair Desk" })).toBeVisible();
    await expect(page.getByText("SIMULATION", { exact: true })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByRole("navigation", { name: "sections" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Scan now/i })).toBeVisible();
    await expect(page.getByLabel("metrics")).toBeVisible();
    await expect(page.getByRole("heading", { name: /Repair tasks/i })).toBeVisible();
  });

  test("health endpoint reports simulation", async ({ request }) => {
    const res = await request.get("/healthz");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body).toMatchObject({ status: "ok", mode: "simulation" });
  });
});
