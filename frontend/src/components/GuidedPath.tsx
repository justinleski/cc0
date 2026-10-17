import type { Work } from "@/api/catalog";
import { WorkTile } from "@/components/WorkTile";

export function GuidedPath({ query, works }: { query: string; works: Work[] }) {
  if (!query || works.length === 0) return null;
  return (
    <section className="mb-10" aria-label={`Results for ${query}`}>
      <h2 className="mb-4 text-xl leading-tight">Results for “{query}”</h2>
      <div className="path-row">
        {works.map((work) => {
          const displayWidth = Math.round(220 * (work.width / work.height));
          return (
            <div key={work.id} style={{ width: displayWidth }}>
              <WorkTile work={work} displayWidth={displayWidth} />
            </div>
          );
        })}
      </div>
    </section>
  );
}
