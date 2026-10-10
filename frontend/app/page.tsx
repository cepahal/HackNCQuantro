"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import InputForm from "../components/InputForm";
import LoadingState from "../components/LoadingState";
import { QuantroApiError, runAnalysis } from "../lib/api";

interface Failure { message: string; code: string | null }

export default function Home() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  async function analyze(url: string) {
    if (busy) return;
    setBusy(true); setFailure(null);
    try {
      const result = await runAnalysis(url);
      router.push(`/results/${encodeURIComponent(result.report_id)}`);
    } catch (error) {
      setFailure(error instanceof QuantroApiError
        ? { message: error.message, code: error.code ?? `HTTP ${error.status}` }
        : { message: error instanceof Error ? error.message : "Analysis failed. Please try again.", code: null });
      setBusy(false);
    }
  }
  return <><section className="hero"><span className="eyebrow">Check the context</span><h1>A clip rarely tells the whole story.</h1>
    <p>Submit an accessible public video or JPG. Quantro searches the indexed source recordings, compares the evidence, and explains what remains uncertain.</p>
    <InputForm busy={busy} onSubmit={analyze}/></section>
    {busy && <LoadingState message="Downloading media and comparing source evidence. Longer recordings can take several minutes; keep this page open."/>}
    {failure && <div role="alert" className="notice error"><strong>{failure.message}</strong>{failure.code && <p>Error code: <code>{failure.code}</code></p>}</div>}</>;
}
