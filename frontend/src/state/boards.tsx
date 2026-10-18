"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import * as api from "@/api/boards";

const TOKEN_KEY = "cc0-local-token";
const SESSION_KEY = "cc0-board-session";
const CREATE_KEY = "cc0-pending-board-create";
type Account = "alice" | "bob";
type PendingCreate = { session: string; key: string; name: string; description: string };
type BoardContextValue = {
  account: Account | null;
  boards: api.BoardDetail[];
  loading: boolean;
  error: string | null;
  busyBoardIds: string[];
  signIn: (account: Account, token: string) => Promise<boolean>;
  signOut: () => void;
  refresh: () => Promise<void>;
  create: (name: string, description: string) => Promise<api.BoardDetail | null>;
  edit: (id: string, name: string, description: string) => Promise<boolean>;
  remove: (id: string) => Promise<boolean>;
  toggleSave: (id: string, assetId: string) => Promise<boolean>;
  moveItem: (id: string, assetId: string, direction: -1 | 1) => Promise<boolean>;
  saveNotes: (id: string, assetId: string, notes: string) => Promise<boolean>;
  /** The current bearer token, for features that call their own endpoints. */
  getToken: () => string;
  recommend: (id: string, mode: api.RecommendationMode, signal: AbortSignal) => Promise<api.BoardRecommendations>;
  clearError: () => void;
};
const BoardContext = createContext<BoardContextValue | null>(null);

function tokenAccount(token: string): Account | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))) as { sub?: string };
    return payload.sub === "local:alice" ? "alice" : payload.sub === "local:bob" ? "bob" : null;
  } catch { return null; }
}

export function BoardProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [boards, setBoards] = useState<api.BoardDetail[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyBoardIds, setBusyBoardIds] = useState<string[]>([]);
  const tokenRef = useRef("");
  const sessionRef = useRef("");
  const generation = useRef(0);
  const readVersion = useRef(0);
  const pending = useRef(new Set<string>());

  function reset() {
    generation.current++;
    readVersion.current++;
    tokenRef.current = "";
    sessionRef.current = "";
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(CREATE_KEY);
    pending.current.clear();
    setBusyBoardIds([]);
    setBoards([]);
    setAccount(null);
    setError(null);
    setLoading(false);
  }
  async function fetchBoards(token: string, current: number, read: number) {
    const summaries = await api.listBoards(token);
    const details = await Promise.all(summaries.map(async (board) => {
      try { return await api.getBoard(token, board.id); }
      catch (cause) {
        if (cause instanceof api.BoardApiError && cause.status === 404) return null;
        throw cause;
      }
    }));
    if (generation.current === current && readVersion.current === read) {
      setBoards(details.filter((board): board is api.BoardDetail => board !== null));
    }
  }
  async function load(token: string) {
    const current = generation.current;
    const read = ++readVersion.current;
    setLoading(true);
    setError(null);
    try { await fetchBoards(token, current, read); }
    catch (cause) {
      if (generation.current === current && readVersion.current === read) {
        if (cause instanceof api.BoardApiError && cause.status === 401) { reset(); setError(cause.message); }
        else setError(cause instanceof Error ? cause.message : "Could not load boards.");
      }
    } finally { if (generation.current === current && readVersion.current === read) setLoading(false); }
  }
  useEffect(() => {
    const saved = sessionStorage.getItem(TOKEN_KEY);
    const savedAccount = saved ? tokenAccount(saved) : null;
    if (saved && savedAccount) {
      tokenRef.current = saved;
      sessionRef.current = sessionStorage.getItem(SESSION_KEY) || crypto.randomUUID();
      sessionStorage.setItem(SESSION_KEY, sessionRef.current);
      setAccount(savedAccount);
      void load(saved);
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(SESSION_KEY);
      sessionStorage.removeItem(CREATE_KEY);
      setLoading(false);
    }
    // Initial session restoration runs once for this provider lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function signIn(selected: Account, token: string) {
    reset();
    const clean = token.trim();
    if (tokenAccount(clean) !== selected) {
      setError(`Paste the token issued for ${selected}.`);
      return false;
    }
    const current = generation.current;
    const read = ++readVersion.current;
    setLoading(true);
    try {
      await fetchBoards(clean, current, read);
      if (generation.current !== current) return false;
      tokenRef.current = clean;
      sessionRef.current = crypto.randomUUID();
      sessionStorage.setItem(TOKEN_KEY, clean);
      sessionStorage.setItem(SESSION_KEY, sessionRef.current);
      setAccount(selected);
      return true;
    } catch (cause) {
      if (generation.current === current) setError(cause instanceof Error ? cause.message : "Could not sign in.");
      return false;
    } finally { if (generation.current === current) setLoading(false); }
  }
  async function mutate(id: string, action: (token: string) => Promise<api.BoardDetail | void>): Promise<{ ok: boolean; board?: api.BoardDetail; status?: number }> {
    if (!tokenRef.current || pending.current.has(id)) return { ok: false };
    pending.current.add(id); setBusyBoardIds([...pending.current]); setError(null);
    ++readVersion.current;
    const current = generation.current;
    const token = tokenRef.current;
    try {
      const result = await action(token);
      if (generation.current !== current) return { ok: false };
      if (result) setBoards((existing) => {
        const found = existing.some((board) => board.id === result.id);
        return found ? existing.map((board) => board.id === result.id ? result : board) : [result, ...existing];
      });
      else setBoards((existing) => existing.filter((board) => board.id !== id));
      return { ok: true, board: result || undefined };
    } catch (cause) {
      if (generation.current === current) {
        if (cause instanceof api.BoardApiError && cause.status === 401) { reset(); setError(cause.message); }
        else {
          setError(cause instanceof Error ? cause.message : "The board request failed.");
          if (cause instanceof api.BoardApiError && [404, 409].includes(cause.status)) {
            void fetchBoards(token, current, ++readVersion.current).catch(() => { /* Keep the original action error visible. */ });
          }
        }
      }
      return { ok: false, status: cause instanceof api.BoardApiError ? cause.status : undefined };
    } finally {
      if (generation.current === current) { pending.current.delete(id); setBusyBoardIds([...pending.current]); setLoading(false); }
    }
  }
  return <BoardContext.Provider value={{
    account, boards, loading, error, busyBoardIds, signIn, signOut: reset,
    refresh: async () => { if (tokenRef.current) await load(tokenRef.current); },
    create: async (name, description) => {
      if (!tokenRef.current || pending.current.has("new")) return null;
      const session = sessionRef.current;
      let pendingCreate: PendingCreate | null = null;
      try { pendingCreate = JSON.parse(sessionStorage.getItem(CREATE_KEY) || "null"); } catch { /* Replace malformed local state. */ }
      if (!pendingCreate || pendingCreate.session !== session || typeof pendingCreate.key !== "string" || pendingCreate.name !== name || pendingCreate.description !== description) {
        pendingCreate = { session, key: crypto.randomUUID(), name, description };
        sessionStorage.setItem(CREATE_KEY, JSON.stringify(pendingCreate));
      }
      const result = await mutate("new", (token) => api.createBoard(token, pendingCreate.key, name, description));
      if (result.ok || [400, 404, 409, 422].includes(result.status || 0)) {
        let stored: PendingCreate | null = null;
        try { stored = JSON.parse(sessionStorage.getItem(CREATE_KEY) || "null"); } catch { /* Leave unrelated state alone. */ }
        if (sessionRef.current === session && stored?.session === session && stored.key === pendingCreate.key) {
          sessionStorage.removeItem(CREATE_KEY);
        }
      }
      return result.board || null;
    },
    edit: async (id, name, description) => (await mutate(id, (token) => api.editBoard(token, id, name, description))).ok,
    remove: async (id) => (await mutate(id, (token) => api.deleteBoard(token, id))).ok,
    toggleSave: async (id, assetId) => {
      const saved = boards.find((board) => board.id === id)?.items.some((item) => item.asset_id === assetId);
      return (await mutate(id, (token) => saved ? api.removePin(token, id, assetId) : api.addPin(token, id, assetId))).ok;
    },
    moveItem: async (id, assetId, direction) => {
      const items = boards.find((board) => board.id === id)?.items;
      if (!items) return false;
      const ids = items.map((item) => item.asset_id);
      const index = ids.indexOf(assetId), next = index + direction;
      if (index < 0 || next < 0 || next >= ids.length) return false;
      [ids[index], ids[next]] = [ids[next], ids[index]];
      return (await mutate(id, (token) => api.reorderPins(token, id, ids))).ok;
    },
    saveNotes: async (id, assetId, notes) => (await mutate(id, (token) => api.editPinNotes(token, id, assetId, notes))).ok,
    getToken: () => tokenRef.current,
    recommend: async (id, mode, signal) => {
      const current = generation.current;
      try { return await api.getRecommendations(tokenRef.current, id, mode, signal); }
      catch (cause) {
        if (generation.current === current && cause instanceof api.BoardApiError && cause.status === 401) { reset(); setError(cause.message); }
        throw cause;
      }
    },
    clearError: () => setError(null),
  }}>{children}</BoardContext.Provider>;
}
export function useBoards() {
  const value = useContext(BoardContext);
  if (!value) throw new Error("useBoards must be used within BoardProvider");
  return value;
}
