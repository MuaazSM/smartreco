import type { Metadata } from "next";

import { AuthProvider } from "../components/AuthProvider";
import { NavBar } from "../components/NavBar";
import { TrackerProvider } from "../components/TrackerProvider";
import "./globals.css";

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
    <html lang="en">
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
