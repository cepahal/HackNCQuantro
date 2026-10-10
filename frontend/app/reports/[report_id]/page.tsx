import { redirect } from "next/navigation";

// The backend's report_url (also sent over iMessage) uses /reports/{id}; /results?id={id} is the canonical page.
export default async function SavedReport({ params }: { params: Promise<{ report_id: string }> }) {
  const { report_id } = await params;
  redirect(`/results?id=${encodeURIComponent(report_id)}`);
}
