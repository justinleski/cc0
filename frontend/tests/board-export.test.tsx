import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BoardDetail } from "@/api/boards";
import * as api from "@/api/exports";
import { BoardExport } from "@/components/BoardExport";

vi.mock("@/api/exports", async (original) => ({
  ...await original<typeof api>(), createExport: vi.fn(), listExports: vi.fn(), getExport: vi.fn(),
}));
vi.mock("@/state/boards", () => ({ useBoards: () => ({ account: "alice", getToken: () => "token" }) }));

const board = (overrides: Partial<BoardDetail> = {}): BoardDetail => ({
  id: "b1", name: "Mood", description: "", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  items: [{ asset_id: "a1", position: 0, notes: "" }], ...overrides,
});
const exported = (overrides: Partial<api.BoardExport> = {}): api.BoardExport => ({
  id: "e1", board_id: "b1", status: "QUEUED", failure_reason: null, included_count: null, excluded_count: null,
  created_at: "2026-01-02T00:00:00Z", completed_at: null, expires_at: null, download_url: null, download_expires_in: null,
  ...overrides,
});
const ready = (overrides: Partial<api.BoardExport> = {}) => exported({
  status: "READY", included_count: 2, excluded_count: 0, expires_at: "2026-01-03T00:00:00Z",
  download_url: "http://store.test/e1.zip", download_expires_in: 60, ...overrides,
});

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); vi.mocked(api.listExports).mockResolvedValue([]); });
afterEach(() => { vi.useRealTimers(); });

it("requests an export, shows progress, then downloads with a fresh link", async () => {
  vi.mocked(api.createExport).mockResolvedValue(exported());
  vi.mocked(api.getExport).mockResolvedValueOnce(exported()).mockResolvedValue(ready());
  const assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, assign });
  render(<BoardExport board={board()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Export board" }));
  expect(await screen.findByText(/Preparing your export/)).toBeTruthy();
  expect((screen.getByRole("button", { name: /Export in progress/ }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(screen.getByText(/Preparing your export/)).toBeTruthy(); // first poll: still queued
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(await screen.findByText(/Your export is ready: 2 works/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Download ZIP" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("http://store.test/e1.zip"));
});

it("does not navigate when rights changed since the archive was built", async () => {
  vi.mocked(api.listExports).mockResolvedValue([ready()]);
  vi.mocked(api.getExport).mockResolvedValue(exported({ status: "FAILED", failure_reason: "rights_changed" }));
  const assign = vi.fn();
  vi.stubGlobal("location", { ...window.location, assign });
  render(<BoardExport board={board()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Download ZIP" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(/no longer available as verified CC0/);
  expect(assign).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Download ZIP" })).toBeNull();
});

it("shows a failed export clearly and lets the owner retry with a new one", async () => {
  vi.mocked(api.listExports).mockResolvedValue([exported({ status: "FAILED", failure_reason: "object_store" })]);
  vi.mocked(api.createExport).mockResolvedValue(exported({ id: "e2" }));
  render(<BoardExport board={board()} />);
  expect((await screen.findByRole("alert")).textContent).toMatch(/file store was unavailable/);
  fireEvent.click(screen.getByRole("button", { name: "Export again" }));
  expect(await screen.findByText(/Preparing your export/)).toBeTruthy();
  expect(api.createExport).toHaveBeenCalledTimes(1);
});

it("reports omitted works and a board that changed after the request", async () => {
  vi.mocked(api.listExports).mockResolvedValue([ready({ included_count: 1, excluded_count: 1 })]);
  render(<BoardExport board={board({ updated_at: "2026-01-05T00:00:00Z" })} />);
  expect(await screen.findByText(/1 left out because it is no longer available/)).toBeTruthy();
  expect(screen.getByText(/board changed after this export was requested/)).toBeTruthy();
});

it("disables export for an empty board and explains request errors", async () => {
  const { rerender } = render(<BoardExport board={board({ items: [] })} />);
  expect((await screen.findByRole("button", { name: "Export board" }) as HTMLButtonElement).disabled).toBe(true);
  vi.mocked(api.createExport).mockRejectedValue(new api.ExportApiError(429));
  rerender(<BoardExport board={board()} />);
  fireEvent.click(screen.getByRole("button", { name: "Export board" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(/already in progress/);
});

it("ignores an export list that arrives after switching boards", async () => {
  let resolveOld!: (items: api.BoardExport[]) => void;
  vi.mocked(api.listExports).mockImplementation((_token, id) => id === "old"
    ? new Promise((resolve) => { resolveOld = resolve; }) : Promise.resolve([]));
  const { rerender } = render(<BoardExport board={board({ id: "old" })} />);
  rerender(<BoardExport board={board({ id: "new" })} />);
  await screen.findByRole("button", { name: "Export board" });
  await act(async () => { resolveOld([ready({ board_id: "old" })]); });
  expect(screen.queryByRole("button", { name: "Download ZIP" })).toBeNull();
});

it("offers a retry when the export list cannot be loaded", async () => {
  vi.mocked(api.listExports).mockRejectedValueOnce(new api.ExportApiError(503)).mockResolvedValue([ready()]);
  render(<BoardExport board={board()} />);
  expect((await screen.findByRole("alert")).textContent).toMatch(/unavailable/);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("button", { name: "Download ZIP" })).toBeTruthy();
});
