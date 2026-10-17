import { expect, test } from "@playwright/test";

test("browse, search, filter, paginate, open provenance and related images against the real API", async ({ page, request }) => {
  const api = process.env.CATALOG_API_URL || "http://127.0.0.1:8000";
  const response = await request.get(`${api}/api/v1/assets?limit=50`);
  expect(response.ok()).toBeTruthy();
  const catalog = await response.json();
  expect(catalog.items.length, "Ingest and embed at least two CC0 assets first").toBeGreaterThan(1);
  const seed = catalog.items.find((item: { title: string | null }) => item.title);
  expect(seed).toBeTruthy();

  await page.goto("/?limit=1");
  await expect(page.getByRole("article")).toHaveCount(1);
  const first = page.locator("article a").first();
  const firstUrl = await first.getAttribute("href");
  await expect(page.locator("article img").first()).toBeVisible();
  await expect.poll(() => page.locator("article img").first().evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await page.getByRole("link", { name: "Next page" }).click();
  await expect(page.locator("article a").first()).not.toHaveAttribute("href", firstUrl!);
  await page.getByRole("link", { name: "First page" }).click();
  await expect(page.locator("article a").first()).toHaveAttribute("href", firstUrl!);

  const search = page.getByRole("searchbox").or(page.getByRole("textbox", { name: "Search works" }));
  await search.fill("blue coastal");
  await search.press("Enter");
  await expect(page.getByText(/Ordered by search relevance/)).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(1);
  await search.fill(`"${seed.title}"`);
  await search.press("Enter");
  await expect(page.locator(`article a[href="/works/${seed.id}"]`)).toBeVisible();
  await expect(page.getByText(/Ordered by search relevance/)).toBeVisible();
  await page.getByText("Filter works", { exact: true }).click();
  await page.getByLabel("Exact location").fill("no such catalog location");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText(/No works match/)).toBeVisible();
  await page.getByRole("link", { name: "Clear search and filters" }).click();
  await page.locator(`article a[href="/works/${seed.id}"]`).click();
  await expect(page.getByRole("heading", { name: seed.title, exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Rights and provenance" })).toBeVisible();
  await expect(page.getByText("CC0 verified", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Rights evidence" })).toHaveAttribute("href", /^https?:/);
  await expect.poll(() => page.locator("main > img").evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  const original = await request.get((await page.getByRole("link", { name: "View original image" }).getAttribute("href"))!);
  expect(original.status()).toBe(200);
  expect(original.headers()["cache-control"]).toBe("no-store");
  await expect(page.locator("article").first()).toBeVisible();
  const relatedUrl = await page.locator("article a").first().getAttribute("href");
  expect(relatedUrl).not.toBe(`/works/${seed.id}`);
  await page.screenshot({ path: "test-results/catalog-provenance.png", fullPage: true });
  await page.locator("article a").first().click();
  await expect(page).toHaveURL(new RegExp(`${relatedUrl}$`));
  await expect(page.getByText("CC0 verified", { exact: true })).toBeVisible();

  await page.goto("/works/00000000-0000-0000-0000-000000000099");
  await expect(page.getByRole("heading", { name: "Work not found" })).toBeVisible();
});
