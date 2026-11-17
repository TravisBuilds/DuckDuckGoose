import type { Metadata } from "next";
import { Fredoka, Nunito } from "next/font/google";
import "./globals.css";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
import MarketingOnly from "@/components/MarketingOnly";

// Self-hosted at build time by next/font: no runtime requests to Google.
const fredoka = Fredoka({ subsets: ["latin"], variable: "--font-fredoka", display: "swap" });
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
    <html lang="en" className={`${fredoka.variable} ${nunito.variable}`}>
      <body className="flex flex-col min-h-screen">
        <MarketingOnly>
          <Header />
        </MarketingOnly>
        {children}
        <Footer />
      </body>
    </html>
  );
}
