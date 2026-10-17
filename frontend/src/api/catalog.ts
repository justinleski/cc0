// Server-side transport and response adaptation. Components never use object keys.
import type { CatalogParams } from "./catalog-query";

type AssetCard = {
  id: string;
  title: string | null;
  creator_id: string | null;
  creator_name: string | null;
  location_label: string | null;
  created_year_start: number | null;
  created_year_end: number | null;
  width: number;
  height: number;
  ingested_at: string;
};
type AssetDetail = AssetCard & {
  description: string | null;
  landing_url: string | null;
  rights_type: string;
  evidence_url: string | null;
  verified_at: string;
};
export type Work = {
  id: string;
  title: string;
  author: string;
  published: string;
  width: number;
  height: number;
  src: string;
  alt: string;
  reasons?: string[];
};
export type WorkDetail = Work & {
  description: string | null;
  location: string | null;
  originalSrc: string;
  sourceUrl: string | null;
  evidenceUrl: string | null;
  rights: string;
  verifiedAt: string;
  ingestedAt: string;
};
export type Creator = { id: string; name: string };

export class CatalogError extends Error {
  constructor(public status: number) {
    super(status === 400 || status === 422
      ? "Check the filters and year range, or start a new search if this page link has expired."
      : "The catalog is temporarily unavailable. Please try again.");
  }
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function request(path: string): Promise<Response> {
  try {
    const base = process.env.CATALOG_API_URL || "http://127.0.0.1:8000";
    return await fetch(`${base.replace(/\/$/, "")}/api/v1${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(30000),
      redirect: "error",
    });
  } catch {
    throw new CatalogError(503);
  }
}

async function json<T>(path: string): Promise<T> {
  const response = await request(path);
  if (!response.ok) throw new CatalogError(response.status);
  return response.json() as Promise<T>;
}

export function safeSourceUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

function imageUrl(id: string, variant: "thumbnail" | "original") {
  return `/api/catalog/${encodeURIComponent(id)}/image/${variant}`;
}

function card(asset: AssetCard): Work {
  const title = asset.title || "Untitled";
  const start = asset.created_year_start;
  const end = asset.created_year_end;
  return {
    id: asset.id, title, author: asset.creator_name || "Unknown creator",
    published: start === null ? String(end ?? "Date unknown")
      : end !== null && start !== end ? `${start}–${end}` : String(start),
    width: asset.width, height: asset.height,
    src: imageUrl(asset.id, "thumbnail"), alt: title,
  };
}

export async function searchCatalog(params: CatalogParams) {
  const result = await json<{ items: AssetCard[]; applied_query: string; suggestion: string | null; next_cursor: string | null }>(
    `/assets?${new URLSearchParams(params)}`,
  );
  return { works: result.items.map(card), query: result.applied_query, suggestion: result.suggestion, nextCursor: result.next_cursor };
}

export async function getWork(id: string): Promise<WorkDetail> {
  if (!uuid.test(id)) throw new CatalogError(404);
  const asset = await json<AssetDetail>(`/assets/${id}`);
  return {
    ...card(asset), description: asset.description, location: asset.location_label,
    originalSrc: imageUrl(id, "original"), sourceUrl: safeSourceUrl(asset.landing_url),
    evidenceUrl: safeSourceUrl(asset.evidence_url), rights: asset.rights_type,
    verifiedAt: asset.verified_at, ingestedAt: asset.ingested_at,
  };
}

const reasons: Record<string, string> = {
  same_creator: "Same creator", same_location: "Same location", same_style_tag: "Shared style tag",
};

export async function relatedWorks(id: string): Promise<Work[]> {
  if (!uuid.test(id)) throw new CatalogError(404);
  const result = await json<{ items: (AssetCard & { reasons: string[] })[] }>(`/assets/${id}/related?limit=12`);
  return result.items.map((asset) => ({ ...card(asset), reasons: asset.reasons.map((reason) => reasons[reason]).filter(Boolean) }));
}

export async function findCreators(query = ""): Promise<Creator[]> {
  const result = await json<{ creators: Creator[] }>(`/creators?${new URLSearchParams({ query, limit: "50" })}`);
  return result.creators;
}

export async function retrieveImage(id: string, variant: string): Promise<Response> {
  const headers = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
  if (!uuid.test(id) || !["thumbnail", "original"].includes(variant)) {
    return new Response("Image not found", { status: 404, headers });
  }
  try {
    const response = await request(`/assets/${id}/image/${variant}`);
    if (!response.ok) {
      return new Response("Image unavailable", { status: response.status === 404 ? 404 : 503, headers });
    }
    const contentType = response.headers.get("content-type") || "";
    if (!["image/jpeg", "image/png", "image/webp"].includes(contentType)) {
      return new Response("Image unavailable", { status: 503, headers });
    }
    // Read before responding so a failed upstream body cannot become a successful image response.
    return new Response(await response.arrayBuffer(), { headers: { ...headers, "Content-Type": contentType } });
  } catch {
    return new Response("Image unavailable", { status: 503, headers });
  }
}
