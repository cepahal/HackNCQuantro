"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { getReport, getDisplayModel, type ReportV2 } from "../lib/api";
import type { DisplayModel } from "../lib/displayModel";
import LoadingState from "./LoadingState";
import ResultCard from "./ResultCard";

export default function ReportPage({ reportId }: { reportId: string }) {
  const [report, setReport] = useState<ReportV2 | null>(null);
  const [display, setDisplay] = useState<DisplayModel | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setReport(null); setDisplay(null); setError("");
    void Promise.all([getReport(reportId), getDisplayModel(reportId)]).then(([data, model]) => { if (active) { setReport(data); setDisplay(model); } }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : "Could not load the report.");
    });
    return () => { active = false; };
  }, [reportId]);
  return <>{error ? <div className="notice error" role="alert">{error} <Link href="/">Check another post</Link></div>
    : report && display ? <ResultCard report={report} display={display}/> : <LoadingState/>}</>;
}
