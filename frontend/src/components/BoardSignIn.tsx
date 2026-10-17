"use client";

import { useState, type FormEvent } from "react";
import { useBoards } from "@/state/boards";

export function BoardSignIn() {
  const { signIn, loading, error } = useBoards();
  const [account, setAccount] = useState<"alice" | "bob">("alice");
  const [token, setToken] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (await signIn(account, token)) setToken("");
  }
  return <section className="mt-8 max-w-xl rounded-control border border-line bg-paper-raised p-5">
    <h2 className="text-xl">Local development sign in</h2>
    <p className="mt-3 text-muted">Generate a one-hour token from the backend terminal with <code>python -m app.auth token {account}</code>, then paste it here. The API verifies the signed token.</p>
    <form onSubmit={(event) => void submit(event)} className="mt-5 flex flex-col gap-4">
      <label>Development account
        <select className="mt-2 block w-full rounded-control border border-line bg-paper px-3 py-2" value={account} onChange={(event) => setAccount(event.target.value as "alice" | "bob") }>
          <option value="alice">Alice</option><option value="bob">Bob</option>
        </select>
      </label>
      <label>Bearer token
        <input className="mt-2 block w-full rounded-control border border-line bg-paper px-3 py-2" type="password" autoComplete="off" required value={token} onChange={(event) => setToken(event.target.value)} />
      </label>
      <button className="self-start rounded-control bg-accent px-4 py-2 text-white disabled:opacity-50" disabled={loading}>Sign in as {account}</button>
    </form>
    {error ? <p role="alert" className="mt-4 text-accent">{error}</p> : null}
  </section>;
}

