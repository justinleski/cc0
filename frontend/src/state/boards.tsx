"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Board = {
  id: string;
  title: string;
  note: string;
  itemIds: string[];
};

const STORAGE_KEY = "cc0-boards";

const seed: Board[] = [
  {
    id: "open-sky",
    title: "Open sky",
    note: "Wide weather, fields, and distance.",
    itemIds: ["11417", "436535", "10154"],
  },
  {
    id: "people",
    title: "People",
    note: "Faces and figures, kept apart from the landscapes.",
    itemIds: ["437397", "436121", "437654"],
  },
];

type BoardContextValue = {
  boards: Board[];
  toggleSave: (boardId: string, workId: string) => void;
  moveItem: (boardId: string, workId: string, direction: -1 | 1) => void;
};

const BoardContext = createContext<BoardContextValue | null>(null);

export function BoardProvider({ children }: { children: ReactNode }) {
  const [boards, setBoards] = useState<Board[]>(seed);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as Board[];
        if (Array.isArray(parsed) && parsed.every((board) => Array.isArray(board.itemIds))) {
          setBoards(parsed);
        }
      } catch {
        sessionStorage.removeItem(STORAGE_KEY);
      }
    }
    setReady(true);
  }, []);

  useEffect(() => {
    if (ready) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(boards));
  }, [boards, ready]);

  const value = useMemo<BoardContextValue>(
    () => ({
      boards,
      toggleSave(boardId, workId) {
        setBoards((current) =>
          current.map((board) => {
            if (board.id !== boardId) return board;
            const has = board.itemIds.includes(workId);
            return {
              ...board,
              itemIds: has
                ? board.itemIds.filter((id) => id !== workId)
                : [...board.itemIds, workId],
            };
          }),
        );
      },
      moveItem(boardId, workId, direction) {
        setBoards((current) =>
          current.map((board) => {
            if (board.id !== boardId) return board;
            const index = board.itemIds.indexOf(workId);
            const next = index + direction;
            if (index < 0 || next < 0 || next >= board.itemIds.length) return board;
            const itemIds = [...board.itemIds];
            const [item] = itemIds.splice(index, 1);
            itemIds.splice(next, 0, item);
            return { ...board, itemIds };
          }),
        );
      },
    }),
    [boards],
  );

  return <BoardContext.Provider value={value}>{children}</BoardContext.Provider>;
}

export function useBoards() {
  const value = useContext(BoardContext);
  if (!value) throw new Error("useBoards must be used within BoardProvider");
  return value;
}
