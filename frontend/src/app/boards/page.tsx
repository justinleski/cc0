"use client";

import { useState, type FormEvent } from "react";
import { BoardSignIn } from "@/components/BoardSignIn";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useBoards } from "@/state/boards";

export default function BoardsPage() {
  const router = useRouter();
  const { account, boards, loading, error, busyBoardIds, create, refresh } = useBoards();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    const board = await create(name, description);
    if (board) { setName(""); setDescription(""); router.push(`/boards/${board.id}`); }
  }
  return <main className="mx-auto max-w-[1000px] px-4 py-10 md:px-10">
    <div className="flex flex-wrap items-end justify-between gap-3"><div>
      <h1 className="text-[length:var(--font-size-title)] leading-[var(--line-title)]">Boards</h1>
      {account ? <p className="mt-2 text-muted">Private boards for {account}</p> : null}
    </div><Link className="text-accent underline" href="/">Browse works</Link></div>
    {loading ? <p role="status" className="mt-8">Loading boards…</p> : null}
    {!loading && !account ? <BoardSignIn /> : null}
    {account ? <>
      <form onSubmit={(event) => void submit(event)} className="mt-8 rounded-control border border-line bg-paper-raised p-5">
        <h2 className="text-xl">Create a board</h2>
        <div className="mt-4 flex flex-col gap-4">
          <label>Board name<input className="mt-2 block w-full rounded-control border border-line bg-paper px-3 py-2" required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>Description<textarea className="mt-2 block w-full rounded-control border border-line bg-paper px-3 py-2" maxLength={5000} rows={2} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
          <button className="self-start rounded-control bg-accent px-4 py-2 text-white disabled:opacity-50" disabled={loading || busyBoardIds.includes("new")}>{busyBoardIds.includes("new") ? "Creating…" : "Create board"}</button>
        </div>
      </form>
      {error ? <div role="alert" className="mt-5 text-accent">{error} <button className="underline" onClick={() => void refresh()}>Retry load</button></div> : null}
      <section className="mt-10"><h2 className="text-xl">Your boards</h2>
        {!loading && boards.length === 0 ? <p className="mt-4 text-muted">No boards yet. Create one, then save works from the catalog.</p> : null}
        <ul className="mt-5 grid gap-4 sm:grid-cols-2">{boards.map((board) => <li key={board.id}>
          <Link href={`/boards/${board.id}`} className="block rounded-control border border-line bg-paper-raised p-5 hover:border-accent">
            <h3 className="text-xl">{board.name}</h3><p className="mt-2 text-muted">{board.description || "No description"}</p>
            <p className="mt-3 text-sm text-accent">{board.items.length} {board.items.length === 1 ? "pin" : "pins"}</p>
          </Link></li>)}</ul>
      </section>
    </> : null}
  </main>;
}
