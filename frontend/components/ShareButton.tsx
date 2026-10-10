"use client";
import { useState } from "react";

function shareUrl(reportId: string): string {
  const base = process.env.NEXT_PUBLIC_REPORT_BASE_URL?.replace(/\/$/, "");
  return base ? `${base}/results/${encodeURIComponent(reportId)}` : window.location.href;
}

export default function ShareButton({ reportId }: { reportId: string }) {
  const [status, setStatus] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(shareUrl(reportId));
      setStatus("Link copied.");
    } catch {
      setStatus("Copy failed. Copy the address bar URL instead.");
    }
  }
  return <div className="toolbar"><span role="status" aria-live="polite">{status}</span>
    <button type="button" onClick={copy}>Share</button></div>;
}
