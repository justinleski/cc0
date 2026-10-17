import { expect, test } from "@playwright/test";

const api = process.env.CATALOG_API_URL || "http://127.0.0.1:8000";
const alice = process.env.E2E_ALICE_TOKEN;
const bob = process.env.E2E_BOB_TOKEN;

test("private boards persist through search, notes, order, reload and account switching", async ({ page, request }) => {
  test.setTimeout(90000);
  expect(alice, "Set E2E_ALICE_TOKEN from the backend token CLI").toBeTruthy();
  expect(bob, "Set E2E_BOB_TOKEN from the backend token CLI").toBeTruthy();
  const catalogResponse = await request.get(`${api}/api/v1/assets?limit=50`);
  expect(catalogResponse.ok()).toBeTruthy();
  const catalog = await catalogResponse.json() as { items: { id: string; title: string | null }[] };
  expect(catalog.items.length).toBeGreaterThanOrEqual(3);
  const ids = catalog.items.slice(0, 3).map((item) => item.id);
  const boardName = `M3 browser board ${Date.now()}`;

  await page.goto("/boards");
  await expect(page.getByRole("heading", { name: "Local development sign in" })).toBeVisible();
  await page.getByLabel("Bearer token").fill(alice!);
  await page.getByRole("button", { name: "Sign in as alice" }).click();
  await expect(page.getByText("Private boards for alice")).toBeVisible();

  // Create and delete an empty board using the confirmation flow.
  await page.getByLabel("Board name").fill(`Temporary ${Date.now()}`);
  await page.getByRole("button", { name: "Create board" }).click();
  await expect(page.getByRole("button", { name: "Delete board" })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete board" }).click();
  await expect(page).toHaveURL(/\/boards$/);

  await page.getByLabel("Board name").fill(boardName);
  await page.getByLabel("Description").fill("Catalog works selected during the M3 acceptance flow.");
  await page.getByRole("button", { name: "Create board" }).click();
  await expect(page).toHaveURL(/\/boards\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: boardName })).toBeVisible();
  const boardUrl = page.url();
  const boardId = boardUrl.split("/").pop()!;
  await page.getByLabel("Name", { exact: true }).fill(`${boardName} edited`);
  await page.getByRole("button", { name: "Save board" }).click();
  await expect(page.getByRole("heading", { name: `${boardName} edited` })).toBeVisible();

  // Search uses the real catalog, then open and pin eligible assets.
  await page.getByRole("textbox", { name: "Search works" }).fill("blue coastal");
  await page.getByRole("textbox", { name: "Search works" }).press("Enter");
  await expect(page.getByRole("main").getByText(/Ordered by search relevance/).first()).toBeVisible();
  await expect(page.getByRole("article").first()).toBeVisible();
  await page.getByRole("article").first().locator("a").first().click();
  await expect(page.getByRole("heading", { name: "Rights and provenance" })).toBeVisible();
  await page.getByRole("button", { name: `Save to ${boardName} edited` }).click();
  await expect(page.getByRole("button", { name: `Saved to ${boardName} edited` })).toBeVisible();
  const searchedId = page.url().split("/").pop()!;
  const pins = [searchedId, ...ids.filter((id) => id !== searchedId).slice(0, 2)];
  for (const id of pins.slice(1)) {
    await page.goto(`/works/${id}`);
    await expect(page.getByRole("heading", { name: "Rights and provenance" })).toBeVisible();
    await page.getByRole("button", { name: `Save to ${boardName} edited` }).click();
    await expect(page.getByRole("button", { name: `Saved to ${boardName} edited` })).toBeVisible();
  }
  await page.goto(boardUrl);
  await expect(page.locator("ol > li")).toHaveCount(3);
  await page.locator("ol > li").first().getByLabel("Pin notes").fill("First observation");
  await Promise.all([
    page.waitForResponse((response) => response.url().includes(`/api/boards/${boardId}/items/`) && response.request().method() === "PATCH"),
    page.locator("ol > li").first().getByRole("button", { name: "Save notes" }).click(),
  ]);
  await expect(page.locator("ol > li").first().getByRole("button", { name: "Save notes" })).toBeDisabled();
  await expect(page.locator("ol > li").first().getByLabel("Pin notes")).toHaveValue("First observation");
  await page.locator("ol > li").first().getByLabel("Pin notes").fill("Edited observation");
  await Promise.all([
    page.waitForResponse((response) => response.url().includes(`/api/boards/${boardId}/items/`) && response.request().method() === "PATCH"),
    page.locator("ol > li").first().getByRole("button", { name: "Save notes" }).click(),
  ]);
  await page.locator("ol > li").nth(2).getByRole("button", { name: "Earlier" }).click();
  const expected = [pins[0], pins[2], pins[1]];
  await expect(page.locator("ol > li > a").first()).toHaveAttribute("href", `/works/${expected[0]}`);
  await page.reload();
  await expect(page.locator("ol > li")).toHaveCount(3);
  for (let i = 0; i < expected.length; i++) {
    await expect(page.locator("ol > li").nth(i).locator("a")).toHaveAttribute("href", `/works/${expected[i]}`);
  }
  await expect(page.locator("ol > li").first().getByLabel("Pin notes")).toHaveValue("Edited observation");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("ol > li").last().getByRole("button", { name: "Remove pin" }).click();
  await expect(page.locator("ol > li")).toHaveCount(2);
  await page.goto(`/works/${expected[2]}`);
  await page.getByRole("button", { name: `Save to ${boardName} edited` }).click();
  await expect(page.getByRole("button", { name: `Saved to ${boardName} edited` })).toBeVisible();
  await page.goto(boardUrl);
  await expect(page.locator("ol > li")).toHaveCount(3);

  // The second account sees no Alice board, and the API independently rejects its UUID.
  await page.getByRole("button", { name: /Switch user \(alice\)/ }).click();
  await expect(page.getByRole("heading", { name: "Local development sign in" })).toBeVisible();
  await page.getByLabel("Development account").selectOption("bob");
  await page.getByLabel("Bearer token").fill(bob!);
  await page.getByRole("button", { name: "Sign in as bob" }).click();
  await expect(page.getByRole("button", { name: /Switch user \(bob\)/ })).toBeVisible();
  await page.goto("/boards");
  await expect(page.getByText("Private boards for bob")).toBeVisible();
  await expect(page.getByRole("link", { name: `${boardName} edited` })).toHaveCount(0);
  await page.goto(boardUrl);
  await expect(page.getByText("Board not found or belongs to another user.")).toBeVisible();
  for (const method of ["GET", "PATCH", "DELETE"]) {
    const response = await request.fetch(`${api}/api/v1/boards/${boardId}`, {
      method, headers: { Authorization: `Bearer ${bob}` },
      ...(method === "PATCH" ? { data: { name: "stolen" } } : {}),
    });
    expect(response.status()).toBe(404);
  }
  const noCredentials = await request.get(`${api}/api/v1/boards/${boardId}`);
  expect(noCredentials.status()).toBe(401);
  const missingAsset = await request.post(`${api}/api/v1/boards/${boardId}/items`, {
    headers: { Authorization: `Bearer ${alice}` }, data: { asset_id: "00000000-0000-0000-0000-000000000099" },
  });
  expect(missingAsset.status()).toBe(409);
  const missingBoard = await request.get(`${api}/api/v1/boards/00000000-0000-0000-0000-000000000099`, {
    headers: { Authorization: `Bearer ${alice}` },
  });
  expect(missingBoard.status()).toBe(404);

  // Abort one request to exercise the visible network error; the subsequent retry is real.
  await page.goto("/boards");
  await page.route("**/api/boards?*", (route) => route.abort());
  await page.reload();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("unavailable");
  await page.unroute("**/api/boards?*");
  await page.getByRole("button", { name: "Retry load" }).click();
  await expect(page.getByText("Private boards for bob")).toBeVisible();
  await page.getByRole("button", { name: /Switch user \(bob\)/ }).click();
  await page.getByLabel("Development account").selectOption("bob");
  await page.getByLabel("Bearer token").fill(`${bob}x`);
  await page.getByRole("button", { name: "Sign in as bob" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("sign-in expired");
  const cleanup = await request.delete(`${api}/api/v1/boards/${boardId}`, {
    headers: { Authorization: `Bearer ${alice}` },
  });
  expect(cleanup.status()).toBe(204);
});
