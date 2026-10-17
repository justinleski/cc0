import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ImageProps } from "next/image";
import * as api from "@/api/catalog";
import BrowsePage from "@/app/page";
import WorkPage from "@/app/works/[id]/page";
import { RelatedWorks } from "@/components/RelatedWorks";
import Loading from "@/app/loading";
import ErrorPage from "@/app/error";
import { CatalogImage } from "@/components/CatalogImage";
import { CatalogFilters } from "@/components/CatalogFilters";

vi.mock("next/image", () => ({ default: ({ unoptimized, priority, ...props }: ImageProps) => {
  void unoptimized; void priority;
  // eslint-disable-next-line @next/next/no-img-element
  return <img {...props} src={String(props.src)} alt={props.alt} />;
} }));
vi.mock("next/navigation", () => ({ notFound: () => { throw new Error("NEXT_NOT_FOUND"); } }));
vi.mock("@/api/catalog", async (original) => ({ ...await original<typeof api>(), searchCatalog: vi.fn(), findCreators: vi.fn(), getWork: vi.fn(), relatedWorks: vi.fn() }));
const work = { id: "one", title: "Harbor", author: "Artist", published: "1800", width: 640, height: 480, src: "/api/catalog/one/image/thumbnail", alt: "Harbor" };
beforeEach(() => {
  vi.mocked(api.findCreators).mockResolvedValue([]);
  vi.mocked(api.relatedWorks).mockResolvedValue([]);
});

it("renders backend order, search relevance and pagination with filters intact", async () => {
  vi.mocked(api.searchCatalog).mockResolvedValue({ works: [work, { ...work, id: "two", title: "Field" }], query: "blue", suggestion: null, nextCursor: "next" });
  render(await BrowsePage({ searchParams: Promise.resolve({ q: "blue", location: "Edo", limit: "2" }) }));
  expect(screen.getByText(/Ordered by search relevance/)).toBeTruthy();
  expect(screen.getAllByRole("article").map(a => a.textContent)).toEqual(["Harbor", "Field"]);
  expect(screen.getByRole("link", { name: "Next page" }).getAttribute("href")).toBe("/?q=blue&location=Edo&limit=2&cursor=next");
  expect(api.searchCatalog).toHaveBeenCalledWith({ q: "blue", location: "Edo", limit: "2" });
});
it("renders empty browse, newest ordering and a first-page link", async () => {
  vi.mocked(api.searchCatalog).mockResolvedValue({ works: [], query: "", suggestion: null, nextCursor: null });
  render(await BrowsePage({ searchParams: Promise.resolve({ cursor: "old" }) }));
  expect(screen.getByText(/No works match/)).toBeTruthy();
  expect(screen.getByText(/Ordered by newest first/)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Next page" })).toBeNull();
  expect(screen.getByRole("link", { name: "First page" }).getAttribute("href")).toBe("/");
});
it("renders catalog failure with retry and keeps creator failures independent", async () => {
  vi.mocked(api.searchCatalog).mockRejectedValue(new api.CatalogError(503));
  vi.mocked(api.findCreators).mockRejectedValue(new Error("unavailable"));
  render(await BrowsePage({ searchParams: Promise.resolve({}) }));
  expect(screen.getByRole("alert").textContent).toContain("temporarily unavailable");
  expect(screen.getByRole("link", { name: "Try again" })).toBeTruthy();
  expect(screen.getByText(/Creator lookup is unavailable/)).toBeTruthy();
});
it("submits exact filters while resetting the cursor", () => {
  render(<CatalogFilters params={{ q: "harbor", cursor: "old", creator_id: "artist", location: "Edo", limit: "2" }} creators={[{ id: "artist", name: "Artist" }]} creatorQuery="" creatorError={false} />);
  fireEvent.change(screen.getByLabelText("From year"), { target: { value: "1800" } });
  const form = screen.getByRole("button", { name: "Apply filters" }).closest("form")!;
  expect(Object.fromEntries(new FormData(form))).toMatchObject({ q: "harbor", creator_id: "artist", location: "Edo", year_from: "1800", limit: "2" });
  expect(new FormData(form).has("cursor")).toBe(false);
});
it("renders asset metadata and source evidence with original retrieval", async () => {
  vi.mocked(api.getWork).mockResolvedValue({ ...work, description: "Painted harbor", location: "Edo", originalSrc: "/api/catalog/one/image/original", sourceUrl: "https://example.org/source", evidenceUrl: "https://example.org/rights", rights: "CC0", verifiedAt: "2026-01-01T00:00:00Z", ingestedAt: "2026-01-02T00:00:00Z" });
  // RelatedWorks is an async server component; test its resolved output separately.
  const page = await WorkPage({ params: Promise.resolve({ id: "one" }) });
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(page);
  expect(html).toContain("Rights and provenance");
  expect(html).toContain("CC0 verified");
  expect(html).toContain("https://example.org/rights");
  expect(html).toContain("/api/catalog/one/image/original");
  expect(html).toContain("Painted harbor");
});
it("handles missing and failed details", async () => {
  vi.mocked(api.getWork).mockRejectedValue(new api.CatalogError(404));
  await expect(WorkPage({ params: Promise.resolve({ id: "missing" }) })).rejects.toThrow("NEXT_NOT_FOUND");
  vi.mocked(api.getWork).mockRejectedValue(new api.CatalogError(503));
  render(await WorkPage({ params: Promise.resolve({ id: "one" }) }));
  expect(screen.getByRole("alert").textContent).toContain("could not be loaded");
});
it("renders related images, empty results and failures", async () => {
  const { unmount } = render(await RelatedWorks({ id: "one" }));
  expect(screen.getByText("No related works are available.")).toBeTruthy();
  unmount();
  vi.mocked(api.relatedWorks).mockResolvedValue([{ ...work, reasons: ["Same creator"] }]);
  const related = render(await RelatedWorks({ id: "one" }));
  expect(screen.getByText("Same creator")).toBeTruthy();
  related.unmount();
  vi.mocked(api.relatedWorks).mockRejectedValue(new Error());
  render(await RelatedWorks({ id: "one" }));
  expect(screen.getByRole("status").textContent).toContain("temporarily unavailable");
});
it("shows image failure and recovers when the source changes", () => {
  const { rerender } = render(<CatalogImage src="/one" alt="Harbor" width={10} height={10} />);
  fireEvent.error(screen.getByRole("img"));
  expect(screen.getByRole("status").textContent).toBe("Image unavailable");
  rerender(<CatalogImage src="/two" alt="Field" width={10} height={10} />);
  expect(screen.getByRole("img").getAttribute("src")).toBe("/two");
});
it("exposes loading status and a working error retry", () => {
  const { unmount } = render(<Loading />);
  expect(screen.getByRole("status").textContent).toContain("Loading catalog");
  unmount();
  const reset = vi.fn();
  render(<ErrorPage reset={reset} />);
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(reset).toHaveBeenCalledOnce();
});
