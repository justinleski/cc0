export type ExportStatus = "QUEUED" | "READY" | "FAILED" | "EXPIRED";
export type BoardExport = {
  id: string;
  board_id: string | null;
  status: ExportStatus;
  failure_reason: string | null;
  included_count: number | null;
  excluded_count: number | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  download_url: string | null;
  download_expires_in: number | null;
};

export class ExportApiError extends Error {
  constructor(public status: number, detail?: string) {
    super(status === 401 ? "Your local sign-in expired. Sign in again."
      : status === 404 ? "Export not found. The board may have been removed."
      : status === 409 ? "None of the saved works can be exported right now."
      : status === 429 ? "Several exports are already in progress. Wait for one to finish."
      : status === 400 ? detail || "This board is too large to export."
      : status === 503 ? "The export service is unavailable. Check the API and retry."
      : "The export request failed. Please try again.");
  }
}

async function request<T>(token: string, path: string, method = "GET", signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError" && signal?.aborted) throw cause;
    throw new ExportApiError(503);
  }
  if (!response.ok) {
    const problem = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new ExportApiError(response.status, typeof problem.detail === "string" ? problem.detail : undefined);
  }
  return response.json() as Promise<T>;
}

export const createExport = (token: string, boardId: string) =>
  request<BoardExport>(token, `/api/boards/${encodeURIComponent(boardId)}/exports`, "POST");
export const listExports = (token: string, boardId: string, signal?: AbortSignal) =>
  request<{ items: BoardExport[] }>(token, `/api/boards/${encodeURIComponent(boardId)}/exports?limit=1`, "GET", signal)
    .then((page) => page.items);
export const getExport = (token: string, id: string, signal?: AbortSignal) =>
  request<BoardExport>(token, `/api/exports/${encodeURIComponent(id)}`, "GET", signal);
