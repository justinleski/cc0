import type { Metadata } from "next";
import { IBM_Plex_Sans } from "next/font/google";
import { Suspense, type ReactNode } from "react";
import { SiteHeader } from "@/components/SiteHeader";
import { BoardProvider } from "@/state/boards";
import "./globals.css";

const plex = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex",
});

export const metadata: Metadata = {
  title: "CC0",
  description: "Browse CC0 works and save them to boards.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={plex.variable}>
      <body>
        <BoardProvider>
          <Suspense fallback={<header className="border-b border-line px-4 py-4 md:px-10">CC0</header>}>
            <SiteHeader />
          </Suspense>
          {children}
        </BoardProvider>
      </body>
    </html>
  );
}
