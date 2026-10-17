import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { CatalogError, getWork } from "@/api/catalog";
import { CatalogImage } from "@/components/CatalogImage";
import { RelatedWorks } from "@/components/RelatedWorks";

export default async function WorkPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let work;
  try {
    work = await getWork(id);
  } catch (error) {
    if (error instanceof CatalogError && error.status === 404) notFound();
    return <main className="mx-auto max-w-[960px] px-4 py-10 md:px-10">
      <p role="alert">This work could not be loaded. Please try again.</p>
      <a className="mt-4 inline-block text-accent" href={`/works/${encodeURIComponent(id)}`}>Try again</a>
      <Link className="ml-6 text-accent" href="/">Browse</Link>
    </main>;
  }

  return (
    <main className="mx-auto max-w-[960px] px-4 py-10 md:px-10">
      <Link href="/">Browse</Link>
      <CatalogImage className="mt-6 w-full bg-paper-raised object-contain" style={{ maxHeight: "70vh" }} src={work.originalSrc} alt={work.alt} width={work.width} height={work.height} sizes="(max-width: 960px) 100vw, 960px" priority />
      <h1 className="mt-6 text-[length:var(--font-size-title)] leading-[var(--line-title)]">{work.title}</h1>
      <p className="mt-4">{work.author}</p>
      <p className="mt-2 text-muted">Created {work.published}</p>
      {work.location ? <p className="mt-2 text-muted">{work.location}</p> : null}
      {work.description ? <p className="mt-4">{work.description}</p> : null}
      <section className="mt-8" aria-labelledby="provenance">
        <h2 id="provenance" className="text-xl">Rights and provenance</h2>
        <p className="mt-4">{work.rights} verified</p>
        <dl className="mt-4 text-muted">
          <dt>Verified</dt><dd><time dateTime={work.verifiedAt}>{new Date(work.verifiedAt).toLocaleString("en-CA", { timeZone: "UTC" })} UTC</time></dd>
          <dt className="mt-2">Ingested</dt><dd><time dateTime={work.ingestedAt}>{new Date(work.ingestedAt).toLocaleString("en-CA", { timeZone: "UTC" })} UTC</time></dd>
        </dl>
        <div className="mt-4 flex flex-wrap gap-6 text-accent">
          {work.sourceUrl ? <a href={work.sourceUrl}>View source</a> : <span>Source link unavailable</span>}
          {work.evidenceUrl ? <a href={work.evidenceUrl}>Rights evidence</a> : <span>Evidence link unavailable</span>}
          <a href={work.originalSrc}>View original image</a>
        </div>
      </section>
      <section className="mt-16">
        <h2 className="mb-6 text-xl leading-tight">Related works</h2>
        <Suspense fallback={<p role="status">Loading related works…</p>}><RelatedWorks id={id} /></Suspense>
      </section>
    </main>
  );
}
