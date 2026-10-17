export const searchFields = ["q", "creator_id", "location", "year_from", "year_to", "limit", "cursor"] as const;
export type CatalogParams = Partial<Record<(typeof searchFields)[number], string>>;

export function catalogParams(raw: Record<string, string | string[] | undefined>): CatalogParams {
  return Object.fromEntries(searchFields.flatMap((key) => {
    const value = raw[key];
    return typeof value === "string" && value.trim() ? [[key, value.trim()]] : [];
  }));
}

export function browseHref(params: CatalogParams, changes: CatalogParams = {}) {
  const query = new URLSearchParams(catalogParams({ ...params, ...changes })).toString();
  return query ? `/?${query}` : "/";
}
