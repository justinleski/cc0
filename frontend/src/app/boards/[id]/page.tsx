"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { Masonry } from "@/components/Masonry";
import { suggestWorks, works, type SuggestMode } from "@/mock/catalog";
import { useBoards } from "@/state/boards";

export default function BoardPage() {
  const params = useParams<{ id: string }>();
  const { boards, moveItem } = useBoards();
  const board = boards.find((item) => item.id === params.id);
  const [mode, setMode] = useState<SuggestMode>("cohesive");

  const saved = useMemo(() => {
    if (!board) return [];
    return board.itemIds
      .map((id) => works.find((work) => work.id === id))
      .filter((work) => work != null);
  }, [board]);

  if (!board) {
    return (
      <main className="mx-auto max-w-[1440px] px-4 py-10 md:px-10">
        <p>That board is not in this browser session.</p>
        <Link href="/boards" className="mt-4 inline-block text-accent">
          Boards
        </Link>
      </main>
    );
  }

  const suggestions = suggestWorks(board.itemIds, mode);
  const modeLabel = mode === "cohesive" ? "More like this board" : "Complement this board";

  return (
    <main className="mx-auto max-w-[1440px] px-4 py-10 md:px-10">
      <Link href="/boards">Boards</Link>
      <h1 className="mt-6 text-[length:var(--font-size-title)] leading-[var(--line-title)]">{board.title}</h1>
      <p className="mt-4 max-w-xl text-muted">{board.note}</p>
      <div className="mt-6 flex gap-6" role="group" aria-label="Suggestions">
        <button
          type="button"
          aria-pressed={mode === "cohesive"}
          className={mode === "cohesive" ? "text-accent" : undefined}
          onClick={() => setMode("cohesive")}
        >
          More like this board
        </button>
        <button
          type="button"
          aria-pressed={mode === "complement"}
          className={mode === "complement" ? "text-accent" : undefined}
          onClick={() => setMode("complement")}
        >
          Complement this board
        </button>
      </div>
      <section className="mt-8 border-t border-line pt-8" aria-label={modeLabel}>
        <h2 className="mb-6 text-xl leading-tight">{modeLabel}</h2>
        {suggestions.length === 0 ? (
          <p className="text-muted">No further works in this mock set.</p>
        ) : (
          <Masonry works={suggestions} />
        )}
      </section>
      <section className="mt-16 border-t border-line pt-8">
        <h2 className="mb-6 text-xl leading-tight">Saved works</h2>
        {saved.length === 0 ? (
          <p className="text-muted">Nothing saved on this board yet.</p>
        ) : (
          <Masonry
            works={saved}
            leadId={saved[0]?.id}
            onMove={(workId, direction) => moveItem(board.id, workId, direction)}
          />
        )}
      </section>
    </main>
  );
}
