import { redirect } from "next/navigation";

// Share links copied before the /results?id= page existed use /results/{id}.
export default async function SharedReport({ params }: { params: Promise<{ reportId: string }> }) {
  const { reportId } = await params;
  redirect(`/results?id=${encodeURIComponent(reportId)}`);
}
