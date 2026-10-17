import { CatalogImage } from "@/components/CatalogImage";
import Link from "next/link";
import type { Work } from "@/api/catalog";

type MoveControls = {
  canEarlier: boolean;
  canLater: boolean;
  onMove: (direction: -1 | 1) => void;
};

export function WorkTile({
  work,
  move,
  displayWidth,
}: {
  work: Work;
  move?: MoveControls;
  displayWidth: number;
}) {
  const sizes = `${Math.ceil(displayWidth)}px`;
  return (
    <article className="work-tile">
      <Link className="work-tile__link" href={`/works/${work.id}`} prefetch={false}>
        <CatalogImage
          className="work-tile__image"
          src={work.src}
          alt={work.alt}
          width={work.width}
          height={work.height}
          sizes={sizes}
        />
        <span className="work-tile__blur-clip" aria-hidden="true">
          <CatalogImage
            className="work-tile__blur"
            src={work.src}
            alt=""
            width={work.width}
            height={work.height}
            sizes={sizes}
          />
        </span>
        <span className="work-tile__scrim" aria-hidden="true" />
        <p className="work-tile__title">{work.title}{work.reasons?.length ? <span className="mt-1 block text-xs">{work.reasons.join(" · ")}</span> : null}</p>
      </Link>
      {move ? (
        <div className="work-tile__moves">
          <button type="button" onClick={() => move.onMove(-1)} disabled={!move.canEarlier}>
            Earlier
          </button>
          <button type="button" onClick={() => move.onMove(1)} disabled={!move.canLater}>
            Later
          </button>
        </div>
      ) : null}
    </article>
  );
}
