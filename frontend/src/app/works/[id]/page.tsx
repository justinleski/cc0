import Image from "next/image";
import Link from "next/link";
import { notFound } from "next/navigation";
import { BoardActions } from "@/components/BoardActions";
import { Masonry } from "@/components/Masonry";
import { getWork, relatedWorks } from "@/mock/catalog";

export default async function WorkPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const work = getWork(id);
  if (!work) notFound();
  const related = relatedWorks(work);

  return (
    <main className="mx-auto max-w-[960px] px-4 py-10 md:px-10">
      <Link href="/">Browse</Link>
      <Image
        className="mt-6 w-full bg-paper-raised object-contain"
        style={{ maxHeight: "70vh" }}
        src={work.src}
        alt={work.alt}
        width={work.width}
        height={work.height}
        sizes="(max-width: 960px) 100vw, 960px"
        priority
      />
      <h1 className="mt-6 text-[length:var(--font-size-title)] leading-[var(--line-title)]">{work.title}</h1>
      <p className="mt-4">{work.author}</p>
      <p className="mt-2 text-muted">Published {work.published}</p>
      <p className="mt-4 text-muted">
        CC0. Open access image from {work.sourceName}.{" "}
        <a className="text-accent" href={work.sourceUrl}>
          View source
        </a>
      </p>
      <BoardActions workId={work.id} />
      {related.length > 0 ? (
        <section className="mt-16">
          <h2 className="mb-6 text-xl leading-tight">Related works</h2>
          <Masonry works={related} />
        </section>
      ) : null}
    </main>
  );
}
