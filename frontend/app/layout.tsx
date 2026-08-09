import type { Metadata } from "next";
import { Fraunces, Inter, JetBrains_Mono } from "next/font/google";

import { AuthProvider } from "../components/AuthProvider";
import { NavBar } from "../components/NavBar";
import { TrackerProvider } from "../components/TrackerProvider";
import "./globals.css";

// Display serif — the "written for you" voice on headlines and the recommendation block.
const display = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
  display: "swap",
});

// Body sans — legibility first, carries the dense UI, forms, tables, reasons.
const sans = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

// Mono — the signature: every observed/measured value (counts, prices, metrics) is set in mono.
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "SmartReco",
  description: "Behavior-aware course recommendations, grounded in a real catalog via RAG.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body>
        <AuthProvider>
          <TrackerProvider>
            <NavBar />
            {children}
          </TrackerProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
