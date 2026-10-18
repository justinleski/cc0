// Same-origin transport for export status. Creating and listing exports goes through
// the board route (`/api/boards/{id}/exports`); only reads by export ID are served here.
async function forward(request: Request, context: { params: Promise<{ path?: string[] }> }) {
  const { path = [] } = await context.params;
  const base = (process.env.CATALOG_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${base}/api/v1/exports${path.length ? `/${path.map(encodeURIComponent).join("/")}` : ""}`, {
      headers: request.headers.get("authorization") ? { Authorization: request.headers.get("authorization")! } : {},
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "Cache-Control": "no-store", ...(upstream.headers.get("content-type") ? { "Content-Type": upstream.headers.get("content-type")! } : {}) },
    });
  } catch {
    return Response.json({ detail: "Export service unavailable" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export const GET = forward;
