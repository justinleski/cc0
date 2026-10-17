"use client";

import Image, { type ImageProps } from "next/image";
import { useState } from "react";

export function CatalogImage(props: ImageProps) {
  const [failedSrc, setFailedSrc] = useState<ImageProps["src"] | null>(null);
  if (failedSrc === props.src) {
    return props.alt ? (
      <span role="status" className="flex h-full min-h-32 items-center justify-center bg-paper-raised p-4 text-muted">
        Image unavailable
      </span>
    ) : null;
  }
  // The optimizer caches bytes and would bypass a later rights withdrawal.
  return <Image {...props} alt={props.alt} unoptimized onError={() => setFailedSrc(props.src)} />;
}
