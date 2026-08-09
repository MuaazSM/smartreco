import type { Metadata } from "next";

import { AdminView } from "../../components/admin/AdminView";

export const metadata: Metadata = {
  title: "Admin · SmartReco",
};

// Server Component shell — the admin/role check and every data fetch need the browser's cookie jar
// (see lib/api.ts), so they all live in `AdminView` ("use client").
export default function AdminPage(): React.ReactElement {
  return <AdminView />;
}
