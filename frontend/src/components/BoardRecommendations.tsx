"use client";

import { useEffect, useRef, useState } from "react";
import type { BoardDetail, BoardRecommendations as Result, RecommendationMode } from "@/api/boards";
import { card, type Work } from "@/api/catalog";
import { Masonry } from "@/components/Masonry";
import { useBoards } from "@/state/boards";

const reasonLabels: Record<string, string> = {
  similar_to_pin: "Like a saved pin", same_creator: "Same creator", same_style_tag: "Shared style tag",
};

// Pin membership, not order or notes, is what changes a recommendation.
const pinKey = (board: BoardDetail) => board.items.map((item) => item.asset_id).sort().join(",");

function works(result: Result): Work[] {
  return result.items.map((item) => ({
    ...card(item), reasons: item.reasons.map((reason) => reasonLabels[reason]).filter(Boolean),
  }));
}

export function BoardRecommendations({ board }: { board: BoardDetail }) {
  const { recommend } = useBoards();
  const [mode, setMode] = useState<RecommendationMode>("cohesive");
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  // Only the newest request may write state, and only if the board it saw is
  // not older than the board this page holds now.
  const latest = useRef(0);
  const known = useRef(board.updated_at);
  known.current = board.updated_at;
  // The provider builds a new function each render; refetching follows pins, not that.
  const fetchRef = useRef(recommend);
  fetchRef.current = recommend;
  const pins = pinKey(board);

  useEffect(() => {
    const request = ++latest.current;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchRef.current(board.id, mode, controller.signal).then((next) => {
      if (request !== latest.current) return;
      if (Date.parse(next.board_version) < Date.parse(known.current)) {
        // The board changed while this request ran. Ask again rather than show it.
        setRetry((count) => count + 1);
        return;
      }
      setResult(next);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (request !== latest.current) return;
      setError(cause instanceof Error ? cause.message : "Suggestions are unavailable.");
      setLoading(false);
    });
    return () => controller.abort();
  }, [board.id, pins, mode, retry]);

  const shown = result?.mode === mode ? result : null;
  return <section className="mt-10" aria-busy={loading}>
    <div className="flex flex-wrap items-end justify-between gap-4">
      <h2 className="text-xl">Suggestions for this board</h2>
      <div role="group" aria-label="Suggestion mode" className="flex gap-2">
        {(["cohesive", "complement"] as const).map((value) => <button
          key={value} type="button" aria-pressed={mode === value}
          className={`rounded-control border px-3 py-1 ${mode === value ? "border-accent text-accent" : "border-line"}`}
          onClick={() => setMode(value)}>
          {value === "cohesive" ? "More like this board" : "Complement (experimental)"}
        </button>)}
      </div>
    </div>
    {shown?.fallback === "browse" ? <p className="mt-3 text-muted">Save a work to get suggestions shaped by this board. Showing recent works for now.</p> : null}
    {shown && shown.ignored_pin_count > 0 ? <p className="mt-3 text-sm text-muted">{shown.ignored_pin_count} saved {shown.ignored_pin_count === 1 ? "pin" : "pins"} did not shape these suggestions: no longer available, or not yet processed for the current image model.</p> : null}
    {error ? <div role="alert" className="mt-5"><p>{error}</p><button type="button" className="mt-2 text-accent underline" onClick={() => setRetry((count) => count + 1)}>Retry</button></div> : null}
    {loading && !shown ? <p role="status" className="mt-5">Finding suggestions…</p> : null}
    {shown && !error ? <div className={`mt-5 ${loading ? "opacity-60" : ""}`}>
      {shown.items.length ? <Masonry works={works(shown)} /> : <p className="text-muted">No suggestions are available yet.</p>}
    </div> : null}
  </section>;
}
