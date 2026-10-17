import Link from "next/link";
import { CatalogError, findCreators, searchCatalog } from "@/api/catalog";
import { browseHref, catalogParams } from "@/api/catalog-query";
import { CatalogFilters } from "@/components/CatalogFilters";
import { GuidedPath } from "@/components/GuidedPath";
import { Masonry } from "@/components/Masonry";

export default async function BrowsePage({ searchParams }: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const raw = await searchParams;
  const params = catalogParams(raw);
  const creatorQuery = typeof raw.creator_query === "string" ? raw.creator_query : "";
  const [search, lookup] = await Promise.allSettled([searchCatalog(params), findCreators(creatorQuery)]);
  const result = search.status === "fulfilled" ? search.value : null;
  const path = result?.query ? result.works.slice(0, 6) : [];
  const wall = result ? result.query ? result.works.slice(6) : result.works : [];

  return (
    <main className="mx-auto max-w-[1440px] px-4 py-10 md:px-10">
      <h1 className="sr-only">Browse</h1>
      <CatalogFilters key={JSON.stringify(raw)} params={params} creators={lookup.status === "fulfilled" ? lookup.value : []} creatorQuery={creatorQuery} creatorError={lookup.status === "rejected"} />
      <p className="mb-6 text-sm text-muted">{result?.query ? "Ordered by search relevance" : "Ordered by newest first"}. Search combines words and visual similarity; use quotes for a required phrase.</p>
      {result ? <>
        {result.query !== (params.q || "") ? <p className="mb-4">Searching for: {result.query || "all works"}</p> : null}
        <GuidedPath query={result.query} works={path} />
        {result.works.length === 0 ? <p>No works match that search on this page.</p> : null}
        {result.suggestion ? <p className="mt-4">Did you mean <Link className="text-accent" href={browseHref(params, { q: result.suggestion, cursor: undefined })}>{result.suggestion}</Link>?</p> : null}
        {wall.length ? <Masonry works={wall} leadId={wall[0]?.id} /> : null}
        <nav className="mt-8 flex gap-6" aria-label="Catalog pages">
          {params.cursor ? <Link href={browseHref(params, { cursor: undefined })} prefetch={false}>First page</Link> : null}
          {result.nextCursor ? <Link href={browseHref(params, { cursor: result.nextCursor })} prefetch={false}>Next page</Link> : null}
        </nav>
      </> : <div role="alert">
        <p>{search.status === "rejected" && search.reason instanceof CatalogError ? search.reason.message : "The catalog is temporarily unavailable. Please try again."}</p>
        <a className="mt-4 inline-block text-accent" href={browseHref(params)}>Try again</a>
      </div>}
      <Link className="mt-6 inline-block text-accent" href="/" prefetch={false}>Clear search and filters</Link>
    </main>
  );
}
