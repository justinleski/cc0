"use client";

import { useBoards } from "@/state/boards";

export function BoardActions({ workId }: { workId: string }) {
  const { boards, toggleSave, moveItem } = useBoards();

  return (
    <div className="mt-6">
      <h2 className="text-xl leading-tight">Boards</h2>
      <ul className="mt-4 flex flex-col gap-3">
        {boards.map((board) => {
          const index = board.itemIds.indexOf(workId);
          const saved = index >= 0;
          return (
            <li key={board.id} className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => toggleSave(board.id, workId)}
                aria-pressed={saved}
                className={
                  saved
                    ? "rounded-control border border-accent px-3 py-2 text-accent"
                    : "rounded-control border border-line px-3 py-2"
                }
              >
                {saved ? `Saved to ${board.title}` : `Save to ${board.title}`}
              </button>
              {saved ? (
                <>
                  <button
                    type="button"
                    className="px-2 py-2 text-muted disabled:opacity-40"
                    onClick={() => moveItem(board.id, workId, -1)}
                    disabled={index === 0}
                  >
                    Earlier
                  </button>
                  <button
                    type="button"
                    className="px-2 py-2 text-muted disabled:opacity-40"
                    onClick={() => moveItem(board.id, workId, 1)}
                    disabled={index === board.itemIds.length - 1}
                  >
                    Later
                  </button>
                </>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
