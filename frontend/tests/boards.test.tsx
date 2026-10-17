import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "@/api/boards";
import { BoardProvider, useBoards } from "@/state/boards";

vi.mock("@/api/boards", async (original) => ({
  ...await original<typeof api>(), listBoards: vi.fn(), getBoard: vi.fn(), createBoard: vi.fn(),
}));
const token = (account: string) => `x.${btoa(JSON.stringify({ sub: `local:${account}` }))}.x`;
const board = (id: string, name: string): api.BoardDetail => ({
  id, name, description: "notes", created_at: "", updated_at: "", items: [],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function Probe() {
  const { account, boards, error, signIn, signOut, create } = useBoards();
  return <>
    <button onClick={() => void signIn("alice", token("alice"))}>Alice</button>
    <button onClick={() => void signIn("bob", token("bob"))}>Bob</button>
    <button onClick={signOut}>Sign out</button>
    <button onClick={() => void create("Repeat name", "notes")}>Create</button>
    <p>Account: {account || "none"}</p>
    <p>Boards: {boards.map((board) => board.name).join(", ")}</p>
    <p>Error: {error || "none"}</p>
  </>;
}

beforeEach(() => { sessionStorage.clear(); vi.clearAllMocks(); });
it("discards an Alice board response that arrives after switching to Bob", async () => {
  let resolveAlice!: (boards: api.BoardSummary[]) => void;
  const aliceList = new Promise<api.BoardSummary[]>((resolve) => { resolveAlice = resolve; });
  vi.mocked(api.listBoards).mockImplementation((value) => value === token("alice") ? aliceList : Promise.resolve([{
    id: "bob-board", name: "Bob board", description: "", created_at: "", updated_at: "",
  }]));
  vi.mocked(api.getBoard).mockImplementation(async (_token, id) => ({
    id, name: id === "bob-board" ? "Bob board" : "Alice board", description: "", created_at: "", updated_at: "", items: [],
  }));
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  fireEvent.click(screen.getByRole("button", { name: "Bob" }));
  await waitFor(() => expect(screen.getByText("Account: bob")).toBeTruthy());
  expect(screen.getByText("Boards: Bob board")).toBeTruthy();
  await act(async () => { resolveAlice([{ id: "alice-board", name: "Alice board", description: "", created_at: "", updated_at: "" }]); await aliceList; });
  expect(screen.getByText("Account: bob")).toBeTruthy();
  expect(screen.getByText("Boards: Bob board")).toBeTruthy();
  expect(sessionStorage.getItem("cc0-local-token")).toBe(token("bob"));
});

it("loads surviving boards when a listed board is deleted before its detail request", async () => {
  vi.mocked(api.listBoards).mockResolvedValue([
    { id: "gone", name: "Gone", description: "", created_at: "", updated_at: "" },
    { id: "live", name: "Live", description: "", created_at: "", updated_at: "" },
  ]);
  vi.mocked(api.getBoard).mockImplementation(async (_token, id) => {
    if (id === "gone") throw new api.BoardApiError(404);
    return { id, name: "Live", description: "", created_at: "", updated_at: "", items: [] };
  });
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  expect(screen.getByText("Boards: Live")).toBeTruthy();
});

it("still rejects sign-in when a detail request fails with a service error", async () => {
  vi.mocked(api.listBoards).mockResolvedValue([
    { id: "live", name: "Live", description: "", created_at: "", updated_at: "" },
  ]);
  vi.mocked(api.getBoard).mockRejectedValue(new api.BoardApiError(503));
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText(/Error: The board service is unavailable/)).toBeTruthy());
  expect(screen.getByText("Account: none")).toBeTruthy();
  expect(sessionStorage.getItem("cc0-local-token")).toBeNull();
});

it("reuses an operation key after an uncertain response and rotates it after success", async () => {
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockRejectedValueOnce(new api.BoardApiError(503))
    .mockImplementation(async (_token, id, name, description) => ({
      id, name, description, created_at: "", updated_at: "", items: [],
    }));
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(screen.getByText(/Error: The board service is unavailable/)).toBeTruthy());
  expect(sessionStorage.getItem("cc0-pending-board-create")).toContain(vi.mocked(api.createBoard).mock.calls[0][1]);
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.createBoard).mock.calls[1][1]).toBe(vi.mocked(api.createBoard).mock.calls[0][1]);
  await waitFor(() => expect(screen.getByText("Boards: Repeat name")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(3));
  expect(vi.mocked(api.createBoard).mock.calls[2][1]).not.toBe(vi.mocked(api.createBoard).mock.calls[1][1]);
});

it("uses a new operation key after a deleted board is reported", async () => {
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockRejectedValueOnce(new api.BoardApiError(404))
    .mockImplementation(async (_token, key, name, description) => ({
      id: key, name, description, created_at: "", updated_at: "", items: [],
    }));
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(sessionStorage.getItem("cc0-pending-board-create")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.createBoard).mock.calls[1][1]).not.toBe(vi.mocked(api.createBoard).mock.calls[0][1]);
});

it("keeps an uncertain create key across provider remount in the same session", async () => {
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockRejectedValueOnce(new api.BoardApiError(503))
    .mockImplementation(async (_token, key) => board(key, "Repeat name"));
  const first = render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(screen.getByText(/Error: The board service is unavailable/)).toBeTruthy());
  const key = vi.mocked(api.createBoard).mock.calls[0][1];
  first.unmount();
  render(<BoardProvider><Probe /></BoardProvider>);
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.createBoard).mock.calls[1][1]).toBe(key);
});

it("keeps Bob's operation key when Alice's old rejection arrives before Bob's uncertain failure", async () => {
  const alice = deferred<api.BoardDetail>();
  const bob = deferred<api.BoardDetail>();
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockImplementationOnce(() => alice.promise)
    .mockImplementationOnce(() => bob.promise)
    .mockImplementation(async (_token, key) => board(key, "Repeat name"));
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(1));
  const aliceKey = vi.mocked(api.createBoard).mock.calls[0][1];
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  fireEvent.click(screen.getByRole("button", { name: "Bob" }));
  await waitFor(() => expect(screen.getByText("Account: bob")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(2));
  const bobKey = vi.mocked(api.createBoard).mock.calls[1][1];
  expect(bobKey).not.toBe(aliceKey);
  await act(async () => { alice.reject(new api.BoardApiError(409)); });
  expect(sessionStorage.getItem("cc0-pending-board-create")).toContain(bobKey);
  expect(screen.getByText("Error: none")).toBeTruthy();
  await act(async () => { bob.reject(new api.BoardApiError(503)); });
  expect(sessionStorage.getItem("cc0-pending-board-create")).toContain(bobKey);
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(3));
  expect(vi.mocked(api.createBoard).mock.calls[2][1]).toBe(bobKey);
  await waitFor(() => expect(screen.getByText("Boards: Repeat name")).toBeTruthy());
});

it("ignores an old successful create after switching accounts", async () => {
  const alice = deferred<api.BoardDetail>();
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockImplementationOnce(() => alice.promise);
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Bob" }));
  await waitFor(() => expect(screen.getByText("Account: bob")).toBeTruthy());
  await act(async () => { alice.resolve(board("alice-board", "Alice board")); });
  expect(screen.getByText(/^Boards:/).textContent).toBe("Boards: ");
  expect(screen.getByText("Account: bob")).toBeTruthy();
  expect(screen.getByText("Error: none")).toBeTruthy();
});

it("isolates a new sign-in to the same account from the previous session", async () => {
  const oldSuccess = deferred<api.BoardDetail>();
  const oldFailure = deferred<api.BoardDetail>();
  const current = deferred<api.BoardDetail>();
  vi.mocked(api.listBoards).mockResolvedValue([]);
  vi.mocked(api.createBoard).mockImplementationOnce(() => oldSuccess.promise)
    .mockImplementationOnce(() => oldFailure.promise)
    .mockImplementationOnce(() => current.promise);
  render(<BoardProvider><Probe /></BoardProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  fireEvent.click(screen.getByRole("button", { name: "Alice" }));
  await waitFor(() => expect(screen.getByText("Account: alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await waitFor(() => expect(vi.mocked(api.createBoard)).toHaveBeenCalledTimes(3));
  const currentKey = vi.mocked(api.createBoard).mock.calls[2][1];
  await act(async () => { oldSuccess.resolve(board("old-board", "Old board")); });
  await act(async () => { oldFailure.reject(new api.BoardApiError(401)); });
  expect(screen.getByText("Account: alice")).toBeTruthy();
  expect(screen.getByText(/^Boards:/).textContent).toBe("Boards: ");
  expect(screen.getByText("Error: none")).toBeTruthy();
  expect(sessionStorage.getItem("cc0-pending-board-create")).toContain(currentKey);
  await act(async () => { current.reject(new api.BoardApiError(503)); });
});
