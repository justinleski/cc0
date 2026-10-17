// Same-origin transport for the private board API. Tokens are issued by the backend CLI.
async function forward(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const { path = [] } = await context.params;
  const url = new URL(request.url);
  const base = (process.env.CATALOG_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${base}/api/v1/boards${path.length ? `/${path.map(encodeURIComponent).join("/")}` : ""}${url.search}`, {
      method: request.method,
      headers: {
        ...(request.headers.get("authorization") ? { Authorization: request.headers.get("authorization")! } : {}),
        ...(request.headers.get("idempotency-key") ? { "Idempotency-Key": request.headers.get("idempotency-key")! } : {}),
        ...(request.method !== "GET" && request.method !== "DELETE" ? { "Content-Type": "application/json" } : {}),
      },
      body: request.method === "GET" || request.method === "DELETE" ? undefined : await request.text(),
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    });
    return new Response(upstream.status === 204 ? null : await upstream.text(), {
      status: upstream.status,
      headers: { "Cache-Control": "no-store", ...(upstream.headers.get("content-type") ? { "Content-Type": upstream.headers.get("content-type")! } : {}) },
    });
  } catch {
    return Response.json({ detail: "Board service unavailable" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const PUT = forward;
export const DELETE = forward;
