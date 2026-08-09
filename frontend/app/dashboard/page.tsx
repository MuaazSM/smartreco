import type { Metadata } from "next";

import { DashboardView } from "../../components/dashboard/DashboardView";

export const metadata: Metadata = {
  title: "Your recommendations · SmartReco",
};

// Server Component shell — all the per-user, cookie-authenticated, live-updating work happens in
// `DashboardView` ("use client"), since a Server Component has no browser cookie jar to authenticate
// with (see lib/api.ts) and this page's entire purpose is client-side interactivity: polling for
// updates, thumbs feedback, the refresh button, the expandable panel.
export default function DashboardPage(): React.ReactElement {
  return <DashboardView />;
}
