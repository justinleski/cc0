"use client";

import Link from "next/link";
import { useBoards } from "@/state/boards";

export default function BoardsPage() {
  const { boards } = useBoards();

  return (
    <main className="mx-auto max-w-[720px] px-4 py-10 md:px-10">
      <h1 className="text-[length:var(--font-size-title)] leading-[var(--line-title)]">Boards</h1>
      <ul className="mt-10">
        {boards.map((board) => (
          <li key={board.id} className="border-t border-line py-6">
            <Link href={`/boards/${board.id}`} className="text-xl leading-tight">
              {board.title}
            </Link>
            <p className="mt-2 text-muted">{board.note}</p>
            <p className="mt-2 text-xs text-muted">
              {board.itemIds.length} {board.itemIds.length === 1 ? "work" : "works"}
            </p>
          </li>
        ))}
      </ul>
    </main>
  );
}
