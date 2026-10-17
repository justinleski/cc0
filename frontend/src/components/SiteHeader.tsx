"use client";

import Link from "next/link";
import { searchFields } from "@/api/catalog-query";
import { usePathname, useSearchParams } from "next/navigation";
import { useBoards } from "@/state/boards";

export function SiteHeader() {
  const pathname = usePathname();
  const params = useSearchParams();
  const query = params.get("q") ?? "";
  const browseCurrent = pathname === "/";
  const boardsCurrent = pathname.startsWith("/boards");
  const { account, signOut } = useBoards();

  return (
    <header className="border-b border-line bg-paper">
      <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-4 px-4 py-4 md:gap-6 md:px-10">
        <Link href="/" className="text-xl leading-tight tracking-tight">
          CC0
        </Link>
        <nav className="flex gap-4" aria-label="Primary">
          <Link href="/" className={browseCurrent ? "text-accent" : undefined} aria-current={browseCurrent ? "page" : undefined}>
            Browse
          </Link>
          <Link
            href="/boards"
            className={boardsCurrent ? "text-accent" : undefined}
            aria-current={boardsCurrent ? "page" : undefined}
          >
            Boards
          </Link>
        </nav>
        <form method="get" className="ml-auto min-w-[12rem] flex-1 md:max-w-sm" action="/" role="search">
          {searchFields.filter((field) => field !== "q" && field !== "cursor").map((field) => params.get(field) ? <input key={field} type="hidden" name={field} value={params.get(field)!} /> : null)}
          <label className="sr-only" htmlFor="q">
            Search works
          </label>
          <input
            key={query}
            id="q"
            name="q"
            defaultValue={query}
            placeholder="Search works"
            className="w-full rounded-control border border-line bg-paper-raised px-3 py-2 text-ink placeholder:text-muted"
          />
        </form>
        {account ? <button type="button" className="text-sm text-accent" onClick={signOut}>Switch user ({account})</button>
          : <Link className="text-sm text-accent" href="/boards">Local sign in</Link>}
      </div>
    </header>
  );
}
