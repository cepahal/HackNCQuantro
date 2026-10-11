"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import ReportPage, { ReportLoading } from "../../components/ReportPage";

function ResultsContent() {
  const params = useSearchParams();
  const jobId = params.get("job");
  const reportId = params.get("id") || params.get("report_id");
  return <ReportPage jobId={jobId} reportId={reportId}/>;
}
export default function ResultsPage() {
  return <Suspense fallback={<main className="page-wrap" style={{paddingTop:80}}><ReportLoading/></main>}><ResultsContent/></Suspense>;
}
