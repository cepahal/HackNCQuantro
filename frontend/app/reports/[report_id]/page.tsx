import ReportPage from "../../../components/ReportPage";

export default async function SavedReport({ params }: { params: Promise<{ report_id: string }> }) {
  const { report_id } = await params;
  return <ReportPage reportId={report_id}/>;
}
