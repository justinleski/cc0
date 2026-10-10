"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { Work } from "@/mock/catalog";
import { WorkTile } from "@/components/WorkTile";

const ROW = 8;

type MoveHandler = (workId: string, direction: -1 | 1) => void;

function columnCount(width: number) {
  if (width >= 1100) return 5;
  if (width >= 720) return 3;
  return 2;
}

export function Masonry({
  works,
  leadId,
  onMove,
}: {
  works: Work[];
  leadId?: string;
  onMove?: MoveHandler;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => setWidth(element.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const columns = width == null ? 2 : columnCount(width);
  const gap = width != null && width >= 720 ? 16 : 12;
  const columnWidth = width == null ? 0 : (width - gap * (columns - 1)) / columns;

  return (
    <div
      ref={ref}
      className="grid"
      style={{
        gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
        gridAutoRows: `${ROW}px`,
        columnGap: gap,
        rowGap: gap,
      }}
    >
      {width == null
        ? null
        : works.map((work, index) => {
            const wide = work.width / work.height > 1.2;
            const spanColumns = work.id === leadId && wide && columns > 2 ? 2 : 1;
            const tileWidth = columnWidth * spanColumns + gap * (spanColumns - 1);
            const tileHeight = tileWidth * (work.height / work.width);
            const span = Math.max(1, Math.ceil((tileHeight + gap) / (ROW + gap)));
            return (
              <div
                key={work.id}
                style={{ gridColumn: `span ${spanColumns}`, gridRow: `span ${span}` }}
              >
                <WorkTile
                  work={work}
                  move={
                    onMove
                      ? {
                          canEarlier: index > 0,
                          canLater: index < works.length - 1,
                          onMove: (direction) => onMove(work.id, direction),
                        }
                      : undefined
                  }
                />
              </div>
            );
          })}
    </div>
  );
}
