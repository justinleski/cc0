import type { Metadata } from "next";
import { IBM_Plex_Sans } from "next/font/google";
import { Suspense, type ReactNode } from "react";
import { SiteHeader } from "@/components/SiteHeader";
import "./globals.css";

const plex = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex",
});

export const metadata: Metadata = {
  title: "CC0",
  description: "Browse verified CC0 works and explore their provenance.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={plex.variable}>
      <body>
          <Suspense fallback={<header className="border-b border-line px-4 py-4 md:px-10">CC0</header>}>
            <SiteHeader />
          </Suspense>
          {children}
      </body>
    </html>
  );
}
