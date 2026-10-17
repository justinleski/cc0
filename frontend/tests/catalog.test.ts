import { describe, expect, it, vi } from "vitest";
import { CatalogError, getWork, relatedWorks, retrieveImage, safeSourceUrl, searchCatalog } from "@/api/catalog";
import { browseHref, catalogParams } from "@/api/catalog-query";

const id = "00000000-0000-0000-0000-000000000001";
const asset = { id, title: "Harbor", creator_name: "Artist", created_year_start: 1800, created_year_end: 1805, width: 640, height: 480, ingested_at: "2026-01-01T00:00:00Z" };
function response(body: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(body));
}

describe("catalog transport", () => {
  it("passes exact filters and cursor to the backend and preserves result ordering", async () => {
    const fetch = response({ items: [asset, { ...asset, id: "second" }], applied_query: '"harbor" blue', next_cursor: "next", suggestion: null });
    const params = { q: '"harbor" blue', creator_id: id, location: "Edo, Japan", year_from: "1800", year_to: "1900", limit: "2", cursor: "opaque+/=" };
    const result = await searchCatalog(params);
    expect(Object.fromEntries(new URL(String(fetch.mock.calls[0][0])).searchParams)).toEqual(params);
    expect(fetch.mock.calls[0][1]).toMatchObject({ cache: "no-store", redirect: "error" });
    expect(result.works.map(work => work.id)).toEqual([id, "second"]);
    expect(result.nextCursor).toBe("next");
    expect(result.works[0].src).toBe(`/api/catalog/${id}/image/thumbnail`);
  });
  it("adapts provenance and related reasons without exposing object keys", async () => {
    const fetch = response({ ...asset, rights_type: "CC0", verified_at: asset.ingested_at, landing_url: "https://example.org/item", evidence_url: "javascript:alert(1)", description: "A harbor", location_label: "Edo", original_key: "private.jpg" });
    const detail = await getWork(id);
    expect(detail).toMatchObject({ rights: "CC0", evidenceUrl: null, sourceUrl: "https://example.org/item", published: "1800–1805", originalSrc: `/api/catalog/${id}/image/original` });
    expect(JSON.stringify(detail)).not.toContain("private.jpg");
    fetch.mockResolvedValue(Response.json({ items: [{ ...asset, reasons: ["same_creator", "unknown"] }] }));
    expect((await relatedWorks(id))[0].reasons).toEqual(["Same creator"]);
  });
  it("handles empty pages, rejected filters, missing assets, and network failures", async () => {
    const fetch = response({ items: [], applied_query: "", next_cursor: null, suggestion: null });
    expect((await searchCatalog({})).works).toEqual([]);
    fetch.mockResolvedValue(new Response("private error", { status: 422 }));
    await expect(searchCatalog({ year_from: "bad" })).rejects.toMatchObject({ status: 422 });
    fetch.mockResolvedValue(new Response(null, { status: 404 }));
    await expect(getWork(id)).rejects.toMatchObject({ status: 404 });
    fetch.mockRejectedValue(new Error("internal address"));
    await expect(searchCatalog({})).rejects.toEqual(new CatalogError(503));
  });
  it("does not send malformed asset identifiers to the backend", async () => {
    const fetch = response({});
    await expect(getWork("../secret")).rejects.toMatchObject({ status: 404 });
    expect((await retrieveImage(id, "../secret")).status).toBe(404);
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([404, 500, 503])("fails closed for upstream image status %s", async (status) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("secret", { status }));
    const result = await retrieveImage(id, "original");
    expect(result.status).toBe(status === 404 ? 404 : 503);
    expect(result.headers.get("cache-control")).toBe("no-store");
    expect(await result.text()).not.toContain("secret");
  });
  it("passes image bytes without caching and rejects non-images or failed bodies", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("image", { headers: { "Content-Type": "image/webp" } }));
    const image = await retrieveImage(id, "thumbnail");
    expect(await image.text()).toBe("image");
    expect(image.headers.get("cache-control")).toBe("no-store");
    fetch.mockResolvedValue(new Response("<html>"));
    expect((await retrieveImage(id, "thumbnail")).status).toBe(503);
    const broken = new Response("image", { headers: { "Content-Type": "image/webp" } });
    vi.spyOn(broken, "arrayBuffer").mockRejectedValue(new Error("stream failed"));
    fetch.mockResolvedValue(broken);
    expect((await retrieveImage(id, "thumbnail")).status).toBe(503);
  });
});

it("builds pagination URLs and resets cursors without dropping filters", () => {
  const params = catalogParams({ q: " harbor ", location: "Edo", cursor: "old", unknown: "ignored", limit: ["2", "3"] });
  expect(params).toEqual({ q: "harbor", location: "Edo", cursor: "old" });
  expect(browseHref(params, { cursor: undefined })).toBe("/?q=harbor&location=Edo");
  expect(browseHref(params, { cursor: "next" })).toContain("cursor=next");
  expect(safeSourceUrl("https://user:password@example.org")).toBeNull();
});
