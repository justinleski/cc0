"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { BoardDetail } from "@/api/boards";
import { createExport, ExportApiError, getExport, listExports, type BoardExport as Export } from "@/api/exports";
import { useBoards } from "@/state/boards";

const POLL_MS = 2000;
const failureText: Record<string, string> = {
  no_eligible_assets: "None of the saved works could be included. Their rights status or files changed.",
  rights_changed: "A work in this export is no longer available as verified CC0, so the archive was withdrawn.",
  object_store: "The file store was unavailable while building the archive.",
  attempts_exhausted: "The export could not be completed after several attempts.",
};
const failureMessage = (reason: string | null) =>
  (reason && failureText[reason]) || "The export could not be completed.";

export function BoardExport({ board }: { board: BoardDetail }) {
  const { account, getToken } = useBoards();
  const [current, setCurrent] = useState<Export | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [reload, setReload] = useState(0);
  // Responses are applied only if the board and account are the ones they were asked for.
  const scope = useRef("");
  scope.current = `${account ?? ""}:${board.id}`;
  const tokenFor = useRef(getToken);
  tokenFor.current = getToken;

  useEffect(() => {
    const mine = scope.current;
    const controller = new AbortController();
    setCurrent(null); setError(null); setLoaded(false);
    if (!account) return;
    listExports(tokenFor.current(), board.id, controller.signal)
      .then((items) => { if (scope.current === mine) { setCurrent(items[0] ?? null); setLoaded(true); } })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || scope.current !== mine) return;
        setError(cause instanceof Error ? cause.message : "Exports are unavailable.");
        setLoaded(true);
      });
    return () => controller.abort();
  }, [account, board.id, reload]);

  const queuedId = current?.status === "QUEUED" ? current.id : null;
  useEffect(() => {
    if (!queuedId) return;
    const mine = scope.current;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      getExport(tokenFor.current(), queuedId, controller.signal).then((next) => {
        if (scope.current !== mine) return;
        setError(null);
        if (next.status === "QUEUED") timer = setTimeout(poll, POLL_MS); else setCurrent(next);
      }).catch((cause: unknown) => {
        if (controller.signal.aborted || scope.current !== mine) return;
        setError(cause instanceof Error ? cause.message : "Could not check export progress.");
        timer = setTimeout(poll, POLL_MS * 2);
      });
    };
    timer = setTimeout(poll, POLL_MS);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [queuedId]);

  const start = useCallback(async () => {
    const mine = scope.current;
    setBusy(true); setError(null);
    try {
      const created = await createExport(tokenFor.current(), board.id);
      if (scope.current === mine) setCurrent(created);
    } catch (cause) {
      if (scope.current === mine) setError(cause instanceof ExportApiError ? cause.message : "The export request failed.");
    } finally {
      if (scope.current === mine) setBusy(false);
    }
  }, [board.id]);

  // The link is short-lived and rights are rechecked on every read, so fetch it at click time.
  const download = useCallback(async () => {
    if (!current) return;
    const mine = scope.current;
    setBusy(true); setError(null);
    try {
      const fresh = await getExport(tokenFor.current(), current.id);
      if (scope.current !== mine) return;
      if (fresh.status === "READY" && fresh.download_url) window.location.assign(fresh.download_url);
      else setCurrent(fresh);
    } catch (cause) {
      if (scope.current === mine) setError(cause instanceof Error ? cause.message : "Download is unavailable.");
    } finally {
      if (scope.current === mine) setBusy(false);
    }
  }, [current]);

  const empty = board.items.length === 0;
  const boardChanged = current && Date.parse(board.updated_at) > Date.parse(current.created_at);
  return <section className="mt-10" aria-labelledby="export-heading" aria-busy={busy}>
    <h2 id="export-heading" className="text-xl">Export</h2>
    <p className="mt-2 text-muted">Download the originals with a manifest and attribution file as one ZIP. Only works that are verified CC0 when the archive is built are included.</p>
    <div className="mt-4 flex flex-wrap items-center gap-3">
      <button type="button" className="rounded-control border border-accent px-4 py-2 text-accent disabled:opacity-40"
        disabled={busy || empty || current?.status === "QUEUED" || !account} onClick={() => void start()}>
        {current?.status === "QUEUED" ? "Export in progress…" : current ? "Export again" : "Export board"}
      </button>
      {empty ? <span className="text-muted">Save a work to enable export.</span> : null}
    </div>
    <div aria-live="polite" className="mt-4">
      {current?.status === "QUEUED" ? <p role="status">Preparing your export. This page updates when it is ready.</p> : null}
      {current?.status === "READY" ? <div>
        <p role="status">Your export is ready: {current.included_count} {current.included_count === 1 ? "work" : "works"}.
          {current.excluded_count ? ` ${current.excluded_count} left out because ${current.excluded_count === 1 ? "it is" : "they are"} no longer available.` : ""}</p>
        <button type="button" className="mt-2 rounded-control border border-line px-4 py-2 disabled:opacity-40" disabled={busy} onClick={() => void download()}>Download ZIP</button>
        {current.expires_at ? <p className="mt-2 text-sm text-muted">Available until {new Date(current.expires_at).toLocaleString()}.</p> : null}
      </div> : null}
      {current?.status === "FAILED" ? <div role="alert"><p>{failureMessage(current.failure_reason)}</p><p className="text-sm text-muted">Use “Export again” to build a fresh archive from the current board.</p></div> : null}
      {current?.status === "EXPIRED" ? <p role="status">This export has expired. Use “Export again” to build a new one.</p> : null}
      {boardChanged && (current?.status === "READY" || current?.status === "QUEUED") ? <p className="mt-2 text-sm text-muted">The board changed after this export was requested. Export again to include the latest pins.</p> : null}
    </div>
    {error ? <div role="alert" className="mt-3"><p>{error}</p>{loaded && !current ? <button type="button" className="mt-2 text-accent underline" onClick={() => setReload((count) => count + 1)}>Retry</button> : null}</div> : null}
  </section>;
}
