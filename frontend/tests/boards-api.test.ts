import { expect, it, vi } from "vitest";
import { createBoard } from "@/api/boards";
import { POST } from "@/app/api/boards/[[...path]]/route";

it("sends the operation key as a header while preserving the legacy create body", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ id: "board" }));
  await createBoard("token", "operation-key", "Board", "notes");
  const init = fetch.mock.calls[0][1]!;
  expect(init.headers).toMatchObject({ "Idempotency-Key": "operation-key" });
  expect(JSON.parse(String(init.body))).toEqual({ name: "Board", description: "notes" });
});

it("forwards the operation key to FastAPI", async () => {
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ id: "board" }));
  const request = new Request("http://localhost/api/boards", {
    method: "POST",
    headers: { Authorization: "Bearer token", "Idempotency-Key": "operation-key" },
    body: JSON.stringify({ name: "Board" }),
  });
  const response = await POST(request, { params: Promise.resolve({ path: [] }) });
  expect(response.status).toBe(200);
  expect(fetch.mock.calls[0][1]?.headers).toMatchObject({ "Idempotency-Key": "operation-key" });
  expect(fetch.mock.calls[0][1]?.body).toBe(JSON.stringify({ name: "Board" }));
});
