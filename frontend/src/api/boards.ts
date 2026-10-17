export type BoardSummary = { id: string; name: string; description: string; created_at: string; updated_at: string };
export type BoardItem = { asset_id: string; position: number; notes: string };
export type BoardDetail = BoardSummary & { items: BoardItem[] };

export class BoardApiError extends Error {
  constructor(public status: number, detail?: string) {
    super(status === 401 ? "Your local sign-in expired. Sign in again."
      : status === 404 ? "Board or pin not found. It may have been removed, or belongs to another user."
      : status === 409 ? detail || "The board changed. Reload it and try again."
      : status === 503 ? "The board service is unavailable. Check local authentication and the API, then retry."
      : status === 422 || status === 400 ? detail || "Check the board details and try again."
      : "The board request failed. Please try again.");
  }
}

async function request<T>(token: string, path: string, method = "GET", body?: object): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/boards${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(30000),
    });
  } catch {
    throw new BoardApiError(503);
  }
  if (!response.ok) {
    const problem = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new BoardApiError(response.status, typeof problem.detail === "string" ? problem.detail : undefined);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export async function listBoards(token: string): Promise<BoardSummary[]> {
  const all: BoardSummary[] = [];
  for (let offset = 0; ; offset += 100) {
    const page = await request<{ items: BoardSummary[] }>(token, `?limit=100&offset=${offset}`);
    all.push(...page.items);
    if (page.items.length < 100) return all;
  }
}
export const getBoard = (token: string, id: string) => request<BoardDetail>(token, `/${encodeURIComponent(id)}`);
export const createBoard = (token: string, name: string, description: string) => request<BoardDetail>(token, "", "POST", { name, description });
export const editBoard = (token: string, id: string, name: string, description: string) => request<BoardDetail>(token, `/${encodeURIComponent(id)}`, "PATCH", { name, description });
export const deleteBoard = (token: string, id: string) => request<void>(token, `/${encodeURIComponent(id)}`, "DELETE");
export const addPin = (token: string, id: string, assetId: string) => request<BoardDetail>(token, `/${encodeURIComponent(id)}/items`, "POST", { asset_id: assetId });
export const removePin = (token: string, id: string, assetId: string) => request<BoardDetail>(token, `/${encodeURIComponent(id)}/items/${encodeURIComponent(assetId)}`, "DELETE");
export const reorderPins = (token: string, id: string, assetIds: string[]) => request<BoardDetail>(token, `/${encodeURIComponent(id)}/items/order`, "PUT", { asset_ids: assetIds });
export const editPinNotes = (token: string, id: string, assetId: string, notes: string) => request<BoardDetail>(token, `/${encodeURIComponent(id)}/items/${encodeURIComponent(assetId)}`, "PATCH", { notes });
