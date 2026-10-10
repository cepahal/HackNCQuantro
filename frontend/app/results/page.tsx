"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import LoadingState from "../../components/LoadingState";
import ReportPage from "../../components/ReportPage";

function ResultsContent() {
  const params = useSearchParams();
  const jobId = params.get("job");
  const reportId = params.get("id") || params.get("report_id");
  return <ReportPage key={`${jobId}:${reportId}`} jobId={jobId} reportId={reportId}/>;
}
export default function ResultsPage() {
  return <Suspense fallback={<main className="page-wrap" style={{paddingTop:80}}><LoadingState job={null}/></main>}><ResultsContent/></Suspense>;
}
