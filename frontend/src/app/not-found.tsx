import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-[720px] px-4 py-10 md:px-10">
      <h1 className="text-[length:var(--font-size-title)] leading-[var(--line-title)]">Work not found</h1>
      <p className="mt-4 text-muted">That work is not available in the public catalog.</p>
      <Link href="/" className="mt-6 inline-block text-accent">
        Browse
      </Link>
    </main>
  );
}
