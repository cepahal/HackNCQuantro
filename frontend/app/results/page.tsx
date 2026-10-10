import Link from "next/link";
import { redirect } from "next/navigation";

export default async function Results({ searchParams }: { searchParams: Promise<{ report_id?: string }> }) {
  const { report_id } = await searchParams;
  if (report_id) redirect(`/results/${encodeURIComponent(report_id)}`);
  return <div className="notice">Choose a saved report or <Link href="/">submit a post</Link>.</div>;
}
