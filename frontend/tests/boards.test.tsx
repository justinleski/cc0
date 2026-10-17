import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "@/api/boards";
import { BoardProvider, useBoards } from "@/state/boards";

vi.mock("@/api/boards", async (original) => ({
  ...await original<typeof api>(), listBoards: vi.fn(), getBoard: vi.fn(),
}));
const token = (account: string) => `x.${btoa(JSON.stringify({ sub: `local:${account}` }))}.x`;
function Probe() {
  const { account, boards, signIn } = useBoards();
  return <>
    <button onClick={() => void signIn("alice", token("alice"))}>Alice</button>
    <button onClick={() => void signIn("bob", token("bob"))}>Bob</button>
    <p>Account: {account || "none"}</p>
    <p>Boards: {boards.map((board) => board.name).join(", ")}</p>
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
