"use client";

import Link from "next/link";
import { useBoards } from "@/state/boards";

export function BoardActions({ workId }: { workId: string }) {
  const { account, boards, loading, busyBoardIds, toggleSave, error } = useBoards();
  return <section className="mt-8" aria-labelledby="save-heading">
    <h2 id="save-heading" className="text-xl">Save to a board</h2>
    {!account ? <p className="mt-3"> <Link className="text-accent underline" href="/boards">Sign in with a local account</Link> to save this work.</p>
      : loading ? <p role="status" className="mt-3">Loading boards…</p>
      : boards.length === 0 ? <p className="mt-3">No boards yet. <Link className="text-accent underline" href="/boards">Create a board</Link>.</p>
      : <ul className="mt-4 flex flex-wrap gap-3">{boards.map((board) => {
        const saved = board.items.some((item) => item.asset_id === workId);
        return <li key={board.id}><button type="button" className={`rounded-control border px-3 py-2 disabled:opacity-50 ${saved ? "border-accent text-accent" : "border-line"}`}
          aria-pressed={saved} disabled={busyBoardIds.includes(board.id)}
          onClick={() => { if (!saved || window.confirm(`Remove this work from ${board.name}?`)) void toggleSave(board.id, workId); }}>
          {busyBoardIds.includes(board.id) ? "Saving…" : saved ? `Saved to ${board.name}` : `Save to ${board.name}`}
        </button></li>;
      })}</ul>}
    {account ? <Link className="mt-3 inline-block text-accent underline" href="/boards">Manage boards</Link> : null}
    {error ? <p role="alert" className="mt-3 text-accent">{error}</p> : null}
  </section>;
}
