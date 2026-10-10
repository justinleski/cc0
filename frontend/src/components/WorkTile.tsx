import Link from "next/link";
import type { Work } from "@/mock/catalog";

type MoveControls = {
  canEarlier: boolean;
  canLater: boolean;
  onMove: (direction: -1 | 1) => void;
};

export function WorkTile({ work, move }: { work: Work; move?: MoveControls }) {
  return (
    <article className="work-tile">
      <Link className="work-tile__link" href={`/works/${work.id}`}>
        <img
          className="work-tile__image"
          src={work.src}
          alt={work.alt}
          width={work.width}
          height={work.height}
        />
        <span className="work-tile__blur-clip" aria-hidden="true">
          <img className="work-tile__blur" src={work.src} alt="" width={work.width} height={work.height} />
        </span>
        <span className="work-tile__scrim" aria-hidden="true" />
        <p className="work-tile__title">{work.title}</p>
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
