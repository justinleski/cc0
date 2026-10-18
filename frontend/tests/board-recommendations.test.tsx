import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { BoardDetail, BoardRecommendations } from "@/api/boards";
import { BoardRecommendations as Rail } from "@/components/BoardRecommendations";

const recommend = vi.fn();
vi.mock("@/state/boards", () => ({ useBoards: () => ({ recommend }) }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
const board = (updated: string, ...pins: string[]): BoardDetail => ({
  id: "board", name: "Board", description: "", created_at: "", updated_at: updated,
  items: pins.map((asset_id, position) => ({ asset_id, position, notes: "" })),
});
const result = (version: string, title: string, mode: "cohesive" | "complement" = "cohesive"): BoardRecommendations => ({
  mode, strategy: "centroid-cosine", fallback: null, profile_pin_count: 1, ignored_pin_count: 0,
  board_version: version,
  items: [{
    id: `id-${title}`, title, creator_id: null, creator_name: null, location_label: null,
    created_year_start: 1800, created_year_end: 1800, width: 640, height: 480,
    ingested_at: "", score: 0.5, reasons: ["similar_to_pin"], closest_pin_id: "a",
  }],
});
const T1 = "2026-10-18T10:00:00.000001+00:00";
const T2 = "2026-10-18T10:00:05.000001+00:00";

beforeEach(() => { recommend.mockReset(); });

it("ignores an older response that arrives after the pins changed", async () => {
  const first = deferred<BoardRecommendations>();
  const second = deferred<BoardRecommendations>();
  recommend.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const view = render(<Rail board={board(T1, "a")} />);
  view.rerender(<Rail board={board(T2, "a", "b")} />);
  expect(recommend).toHaveBeenCalledTimes(2);
  expect((recommend.mock.calls[0][2] as AbortSignal).aborted).toBe(true);

  await act(async () => { second.resolve(result(T2, "Newer")); await second.promise; });
  await act(async () => { first.resolve(result(T1, "Older")); await first.promise; });
  expect(screen.getByText("Newer")).toBeTruthy();
  expect(screen.queryByText("Older")).toBeNull();
  expect(screen.getByText("Like a saved pin")).toBeTruthy();
});

it("asks again when the response describes an older board than the page holds", async () => {
  recommend
    .mockResolvedValueOnce(result(T1, "Stale"))
    .mockResolvedValueOnce(result(T2, "Fresh"));
  render(<Rail board={board(T2, "a")} />);
  await waitFor(() => expect(screen.getByText("Fresh")).toBeTruthy());
  expect(recommend).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Stale")).toBeNull();
});

it("does not refetch for note-only changes and refetches on mode change", async () => {
  recommend.mockImplementation(async (_id: string, mode: "cohesive" | "complement") =>
    result(T2, mode === "cohesive" ? "Alike" : "Contrast", mode));
  const view = render(<Rail board={board(T1, "a")} />);
  await waitFor(() => expect(screen.getByText("Alike")).toBeTruthy());
  view.rerender(<Rail board={board(T2, "a")} />);
  expect(recommend).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Complement (experimental)" }));
  await waitFor(() => expect(screen.getByText("Contrast")).toBeTruthy());
  expect(recommend.mock.calls[1][1]).toBe("complement");
});
