import type { AnalysisResult, DisplayModel, ReportMedia, ReportV2 } from "./contracts";
export type { AnalysisResult, ReportMedia, ReportV2 } from "./contracts";

function apiBase(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
}

function reportApiBase(): string {
  if (typeof window !== "undefined" && ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) {
    return apiBase();
  }
  return (process.env.NEXT_PUBLIC_REPORT_API_BASE_URL || apiBase()).replace(/\/$/, "");
}

async function request(path: string, options?: RequestInit, base = apiBase()): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { ...options, cache: "no-store" });
  } catch {
    throw new Error("Could not reach the analysis service. Check that the backend is running.");
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = data as { error?: { message?: unknown }; detail?: unknown } | null;
    const message = error?.error?.message;
    throw new Error(typeof message === "string" ? message : `The request failed (${response.status}).`);
  }
  return data;
}

export async function startAnalysis(url: string, jobId: string, conversationId?: string): Promise<AnalysisResult> {
  const data = await request(`/analyze/${encodeURIComponent(jobId)}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, ...(conversationId ? { conversation_id: conversationId } : {}) }),
  });
  const result = data as AnalysisResult | null;
  if (!result || result.job_id !== jobId || !/^report_[A-Za-z0-9_-]{1,128}$/.test(result.report_id)
      || !["matched", "no_verified_match"].includes(result.outcome) || !Array.isArray(result.warnings)) {
    throw new Error("The analysis service returned an invalid result.");
  }
  return result;
}

export async function getReport(reportId: string): Promise<ReportV2> {
  if (!/^report_[A-Za-z0-9_-]{1,128}$/.test(reportId)) throw new Error("The report identifier is invalid.");
  const data = await request(`/reports/${encodeURIComponent(reportId)}`, undefined, reportApiBase());
  const report = data as ReportV2 | null;
  if (!report || report.schema_version !== "2.1" || !report.submitted || !Array.isArray(report.limitations)
      || !["matched", "no_verified_match"].includes(report.outcome)) {
    throw new Error("The analysis service returned an invalid report.");
  }
  return report;
}

export function publicUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, apiBase());
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function mediaUrl(media: Pick<ReportMedia["post"]["media"], "media_id" | "mime_type" | "playback_url">): string | null {
  return publicUrl(media.playback_url) || (/^[A-Za-z0-9_-]{1,128}$/.test(media.media_id)
    ? `${reportApiBase()}/media/${encodeURIComponent(media.media_id)}` : null);
}

export async function runAnalysis(url: string, conversationId?: string): Promise<AnalysisResult> {
  return startAnalysis(url, `job_${crypto.randomUUID()}`, conversationId);
}

export async function getDisplayModel(reportId: string): Promise<DisplayModel> {
  if (!/^report_[A-Za-z0-9_-]{1,128}$/.test(reportId)) throw new Error("The report identifier is invalid.");
  const data = await request(`/reports/${encodeURIComponent(reportId)}/display`, undefined, reportApiBase());
  const model = data as DisplayModel | null;
  if (!model || typeof model.confidence_note !== "string" || !model.submitted_panel || !Array.isArray(model.limitations)) {
    throw new Error("The analysis service returned an invalid display model.");
  }
  return model;
}
