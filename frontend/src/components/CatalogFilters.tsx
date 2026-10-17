import type { Creator } from "@/api/catalog";
import type { CatalogParams } from "@/api/catalog-query";

const inputClass = "rounded-control border border-line bg-paper-raised px-3 py-2";

export function CatalogFilters({ params, creators, creatorQuery, creatorError }: {
  params: CatalogParams; creators: Creator[]; creatorQuery: string; creatorError: boolean;
}) {
  return (
    <details className="mb-8 border-b border-line pb-4" open={Boolean(params.creator_id || params.location || params.year_from || params.year_to || creatorQuery)}>
      <summary className="cursor-pointer">Filter works</summary>
      <form method="get" action="/" className="mt-4 flex flex-wrap items-end gap-4">
        {Object.entries(params).filter(([key]) => key !== "cursor").map(([key, value]) => <input key={key} type="hidden" name={key} value={value} />)}
        <label className="flex flex-col gap-2">Find a creator
          <input className={inputClass} name="creator_query" defaultValue={creatorQuery} />
        </label>
        <button className={inputClass} type="submit">Find creator</button>
      </form>
      {creatorError ? <p role="status" className="mt-4">Creator lookup is unavailable. Other filters still work.</p> : null}
      <form method="get" action="/" className="mt-4 flex flex-wrap items-end gap-4">
        <input type="hidden" name="q" value={params.q || ""} />
        <input type="hidden" name="creator_query" value={creatorQuery} />
        <label className="flex flex-col gap-2">Creator
          <select className={inputClass} name="creator_id" defaultValue={params.creator_id || ""}>
            <option value="">All creators</option>
            {params.creator_id && !creators.some((creator) => creator.id === params.creator_id) ? <option value={params.creator_id}>Selected creator</option> : null}
            {creators.map((creator) => <option key={creator.id} value={creator.id}>{creator.name}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-2">Exact location
          <input className={inputClass} name="location" defaultValue={params.location || ""} />
        </label>
        <label className="flex flex-col gap-2">From year
          <input className={`${inputClass} w-28`} name="year_from" type="number" step="1" defaultValue={params.year_from || ""} />
        </label>
        <label className="flex flex-col gap-2">To year
          <input className={`${inputClass} w-28`} name="year_to" type="number" step="1" defaultValue={params.year_to || ""} />
        </label>
        <label className="flex flex-col gap-2">Works per page
          <input className={`${inputClass} w-28`} name="limit" type="number" min="1" max="50" defaultValue={params.limit || "20"} />
        </label>
        <button className={inputClass} type="submit">Apply filters</button>
      </form>
    </details>
  );
}
