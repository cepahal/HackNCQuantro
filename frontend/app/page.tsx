"use client";
import { useState } from "react";
import Link from "next/link";
import InputForm from "../components/InputForm";
import LoadingState from "../components/LoadingState";
import ResultCard from "../components/ResultCard";
import { getReport, startAnalysis, type AnalysisResult, type ReportV2 } from "../lib/api";

export default function Home() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [report, setReport] = useState<ReportV2 | null>(null);
  async function analyze(url: string) {
    if (busy) return;
    setBusy(true); setError(""); setResult(null); setReport(null);
    try {
      const next = await startAnalysis(url, crypto.randomUUID());
      setResult(next);
      setReport(await getReport(next.report_id));
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Analysis failed. Please try again."); }
    finally { setBusy(false); }
  }
  return <><section className="hero"><span className="eyebrow">Check the context</span><h1>A clip rarely tells the whole story.</h1>
    <p>Submit an accessible public video or JPG. Quantro searches the indexed source recordings, compares the evidence, and explains what remains uncertain.</p>
    <InputForm busy={busy} onSubmit={analyze}/></section>
    {busy && <LoadingState message="Downloading media and comparing source evidence. Longer recordings can take several minutes; keep this page open."/>}
    {error && <div role="alert" className="notice error">{error}</div>}
    {result && <div className="notice"><Link href={`/reports/${result.report_id}`}>Open saved report ↗</Link>
      {result.delivery_warning && <p>{result.delivery_warning}</p>}
      {result.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div>}
    {report && <ResultCard report={report}/>}</>;
}
