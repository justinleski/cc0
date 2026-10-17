import Link from "next/link";

export default function BoardsPage() {
  return <main className="mx-auto max-w-[720px] px-4 py-10 md:px-10">
    <h1 className="text-[length:var(--font-size-title)] leading-[var(--line-title)]">Boards</h1>
    <p className="mt-6 text-muted">Boards will be available in a later slice. Browse the catalog to explore verified CC0 works.</p>
    <Link className="mt-4 inline-block text-accent" href="/">Browse</Link>
  </main>;
}
