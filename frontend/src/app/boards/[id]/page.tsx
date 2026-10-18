"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import type { WorkDetail } from "@/api/catalog";
import { CatalogImage } from "@/components/CatalogImage";
import { useBoards } from "@/state/boards";
import { BoardSignIn } from "@/components/BoardSignIn";
import { BoardExport } from "@/components/BoardExport";

function Pin({ boardId, assetId, notes, position, count, busy }: {
  boardId: string; assetId: string; notes: string; position: number; count: number; busy: boolean;
}) {
  const { moveItem, toggleSave, saveNotes } = useBoards();
  const [work, setWork] = useState<WorkDetail | null>(null);
  const [draft, setDraft] = useState(notes);
  useEffect(() => { setDraft(notes); }, [notes]);
  useEffect(() => {
    let live = true;
    fetch(`/api/works/${encodeURIComponent(assetId)}`, { cache: "no-store" })
      .then((response) => response.ok ? response.json() as Promise<WorkDetail> : null)
      .then((result) => { if (live) setWork(result); })
      .catch(() => { if (live) setWork(null); });
    return () => { live = false; };
  }, [assetId]);
  return <li className="rounded-control border border-line bg-paper-raised p-4">
    <Link href={`/works/${assetId}`} className="block">
      <CatalogImage className="aspect-[4/3] w-full rounded-control bg-paper object-cover" src={`/api/catalog/${assetId}/image/thumbnail`} alt={work?.title || "Saved work"} width={work?.width || 640} height={work?.height || 480} sizes="(max-width: 640px) 100vw, 320px" />
      <h3 className="mt-3 text-lg">{work?.title || "Saved work"}</h3>
      {work ? <p className="text-sm text-muted">{work.author}</p> : null}
    </Link>
    <div className="mt-3 flex flex-wrap gap-2">
      <button type="button" className="rounded-control border border-line px-3 py-1 disabled:opacity-40" disabled={busy || position === 0} onClick={() => void moveItem(boardId, assetId, -1)}>Earlier</button>
      <button type="button" className="rounded-control border border-line px-3 py-1 disabled:opacity-40" disabled={busy || position === count - 1} onClick={() => void moveItem(boardId, assetId, 1)}>Later</button>
      <button type="button" className="rounded-control border border-line px-3 py-1 text-accent disabled:opacity-40" disabled={busy} onClick={() => { if (window.confirm("Remove this pin from the board?")) void toggleSave(boardId, assetId); }}>Remove pin</button>
    </div>
    <form className="mt-4" onSubmit={(event) => { event.preventDefault(); void saveNotes(boardId, assetId, draft); }}>
      <label htmlFor={`notes-${assetId}`} className="text-sm">Pin notes</label>
      <textarea id={`notes-${assetId}`} className="mt-2 block w-full rounded-control border border-line bg-paper p-2 disabled:opacity-50" rows={3} maxLength={5000} disabled={busy} value={draft} onChange={(event) => setDraft(event.target.value)} />
      <button className="mt-2 rounded-control border border-accent px-3 py-1 text-accent disabled:opacity-40" disabled={busy || draft === notes}>{busy ? "Saving…" : "Save notes"}</button>
    </form>
  </li>;
}

export default function BoardPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { account, boards, loading, error, busyBoardIds, edit, remove, refresh } = useBoards();
  const board = boards.find((item) => item.id === id);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  useEffect(() => { if (board) { setName(board.name); setDescription(board.description); } }, [board]);
  async function save(event: FormEvent) { event.preventDefault(); await edit(id, name, description); }
  async function destroy() {
    if (window.confirm(`Delete ${board?.name || "this board"} and all its pins?`)) {
      if (await remove(id)) router.push("/boards");
    }
  }
  const busy = loading || busyBoardIds.includes(id);
  return <main className="mx-auto max-w-[1100px] px-4 py-10 md:px-10">
    <Link className="text-accent underline" href="/boards">All boards</Link>
    {loading ? <p role="status" className="mt-8">Loading board…</p> : null}
    {!loading && !account ? <BoardSignIn /> : null}
    {account && !loading && !board ? <div role="alert" className="mt-8">
      <p>{error || "Board not found or belongs to another user."}</p>
      <button type="button" className="mt-3 text-accent underline" onClick={() => void refresh()}>Retry</button>
    </div> : null}
    {board ? <>
      <div className="mt-6 flex flex-wrap items-start justify-between gap-4">
        <div><h1 className="text-[length:var(--font-size-title)] leading-[var(--line-title)]">{board.name}</h1><p className="mt-2 text-muted">{board.description || "No description"}</p></div>
        <button type="button" className="text-accent underline disabled:opacity-40" disabled={busy} onClick={() => void destroy()}>Delete board</button>
      </div>
      {error ? <p role="alert" className="mt-5 text-accent">{error}</p> : null}
      <form className="mt-8 rounded-control border border-line bg-paper-raised p-5" onSubmit={(event) => void save(event)}>
        <h2 className="text-xl">Edit board</h2>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label>Name<input className="mt-2 block w-full rounded-control border border-line bg-paper p-2" required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>Description<textarea className="mt-2 block w-full rounded-control border border-line bg-paper p-2" rows={2} maxLength={5000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        </div>
        <button className="mt-4 rounded-control border border-accent px-4 py-2 text-accent disabled:opacity-40" disabled={busy || (name === board.name && description === board.description)}>{busy ? "Saving…" : "Save board"}</button>
      </form>
      <section className="mt-10"><div className="flex flex-wrap items-end justify-between gap-4"><h2 className="text-xl">Pins ({board.items.length})</h2><Link className="text-accent underline" href="/">Find more works</Link></div>
        {board.items.length === 0 ? <p className="mt-5 text-muted">This board is empty. Browse the catalog and save a verified CC0 work.</p> : null}
        <ol className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">{board.items.map((item, position) => <Pin key={item.asset_id} boardId={id} assetId={item.asset_id} notes={item.notes} position={position} count={board.items.length} busy={busy} />)}</ol>
      </section>
      <BoardExport board={board} />
    </> : null}
  </main>;
}
