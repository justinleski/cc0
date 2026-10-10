import { GuidedPath } from "@/components/GuidedPath";
import { Masonry } from "@/components/Masonry";
import { filterWorks } from "@/mock/catalog";

export default async function BrowsePage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const query = q?.trim() ?? "";
  const matches = filterWorks(query);
  const path = query ? matches.slice(0, 6) : [];
  const wall = query ? matches.slice(6) : matches;
  const leadId = (wall[0] ?? matches[0])?.id;

  return (
    <main className="mx-auto max-w-[1440px] px-4 py-10 md:px-10">
      <h1 className="sr-only">Browse</h1>
      <GuidedPath query={query} works={path} />
      {matches.length === 0 ? (
        <p>No works match that search.</p>
      ) : wall.length > 0 ? (
        <Masonry works={wall} leadId={leadId} />
      ) : null}
    </main>
  );
}
