"use client";

export default function CatalogErrorPage({ reset }: { reset: () => void }) {
  return <main className="mx-auto max-w-[960px] px-4 py-10 md:px-10">
    <p role="alert">The catalog could not be loaded.</p>
    <button className="mt-4 text-accent" onClick={reset}>Try again</button>
  </main>;
}
