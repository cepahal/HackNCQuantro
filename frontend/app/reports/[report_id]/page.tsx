import ReportPage from "../../../components/ReportPage";

// Render this stable path directly so read-only report gateways need no redirects.
export default async function SavedReport({ params }: { params: Promise<{ report_id: string }> }) {
  const { report_id } = await params;
  return <ReportPage key={report_id} reportId={report_id} jobId={null}/>;
}
