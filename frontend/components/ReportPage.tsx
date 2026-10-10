"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { getReport, QuantroApiError } from "../lib/api";
import type { ReportV2 } from "../lib/api-types";
import LoadingState from "./LoadingState";
import ResultCard from "./ResultCard";

export default function ReportPage({ reportId }: { reportId: string }) {
  const [report, setReport] = useState<ReportV2 | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void getReport(reportId).then((data) => { if (active) setReport(data); }).catch((failure: unknown) => {
      if (!active) return;
      const message = failure instanceof Error ? failure.message : "Could not load the report.";
      setError(failure instanceof QuantroApiError && failure.code ? `${message} (${failure.code})` : message);
    });
    return () => { active = false; };
  }, [reportId]);
  return <>{error ? <div className="notice error" role="alert">{error} <Link href="/">Check another post</Link></div>
    : report ? <ResultCard report={report}/> : <LoadingState/>}</>;
}
