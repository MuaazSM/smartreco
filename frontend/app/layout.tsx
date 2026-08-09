import type { Metadata } from "next";
import { Fraunces, Inter, JetBrains_Mono } from "next/font/google";

import { AuthProvider } from "../components/AuthProvider";
import { NavBar } from "../components/NavBar";
import { TrackerProvider } from "../components/TrackerProvider";
import "./globals.css";

// Display serif — the "written for you" voice on headlines and the recommendation block.
// Optical sizing (opsz) tracks the font-size for free; SOFT/WONK are dialed in globals.css so
// large headlines read warm and hand-cut rather than templated. Italic backs the `<em>` accent.
const display = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
  display: "swap",
  style: ["normal", "italic"],
  axes: ["opsz", "SOFT", "WONK"],
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
  title: "SmartReco — the recommender that shows its work",
  description:
    "Behavior-aware course recommendations, grounded in a real catalog via RAG. It watches what a learner does, retrieves real courses, grades its own picks, and won't suggest a thing it can't cite.",
};

// Runs before first paint: apply a previously chosen theme so there's no light/dark flash on
// reload. No stored choice → the CSS falls back to prefers-color-scheme. Kept tiny and inline.
const themeInit = `(function(){try{var t=localStorage.getItem('theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}}catch(e){}})();`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${display.variable} ${sans.variable} ${mono.variable}`}
    >
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
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
