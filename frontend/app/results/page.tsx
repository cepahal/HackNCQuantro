import Link from "next/link";
import ReportPage from "../../components/ReportPage";

export default async function Results({ searchParams }: { searchParams: Promise<{ report_id?: string }> }) {
  const { report_id } = await searchParams;
  return report_id ? <ReportPage reportId={report_id}/> : <div className="notice">Choose a saved report or <Link href="/">submit a post</Link>.</div>;
}
