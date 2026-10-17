import { CatalogError, getWork } from "@/api/catalog";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  try {
    return Response.json(await getWork(id), { headers: { "Cache-Control": "no-store" } });
  } catch (cause) {
    return Response.json({ detail: "Work unavailable" }, {
      status: cause instanceof CatalogError && cause.status === 404 ? 404 : 503,
      headers: { "Cache-Control": "no-store" },
    });
  }
}
