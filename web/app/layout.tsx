import type { Metadata } from "next";
import { Montserrat, Nunito } from "next/font/google";
import "./globals.css";
import { SiteFooter, SiteHeader } from "@/components/SiteChrome";

// Self-hosted at build time by next/font: no runtime requests to Google.
const montserrat = Montserrat({ subsets: ["latin"], variable: "--font-montserrat", display: "swap" });
const nunito = Nunito({ subsets: ["latin"], variable: "--font-nunito", display: "swap" });

export const metadata: Metadata = {
  title: "DuckDuckGoose - AI Video Generation",
  description: "Pick a card, get a finished short video. Create original content with our hands-free AI video harness.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${montserrat.variable} ${nunito.variable}`}>
      <body className="flex flex-col min-h-screen">
        <SiteHeader />
        {children}
        <SiteFooter />
      </body>
    </html>
  );
}
