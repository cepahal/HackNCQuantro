export interface AnalysisResult {
  job_id: string;
  outcome: "matched" | "no_verified_match";
  report_id: string;
  report_url: string;
  delivery_status: "accepted" | "failed" | "skipped";
  delivery_warning: string | null;
  warnings: string[];
}

export interface TranscriptSentence {
  sentence_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
}

export interface ReportMedia {
  post: {
    post_url: string;
    title: string | null;
    caption: string | null;
    published_at: string | null;
    author: {
      handle: string | null; display_name: string | null;
      followers_count: number | null; following_count: number | null; posts_count: number | null;
    };
    media: { media_id: string; mime_type: string | null; playback_url: string | null };
  };
  transcript: { status: string; sentences: TranscriptSentence[] };
}

export interface Confidence {
  level: "high" | "moderate" | "low" | "not_assessed";
  basis: string[];
  limitations: string[];
}

export interface ReportV2 {
  schema_version: "2.1";
  job_id: string;
  outcome: "matched" | "no_verified_match";
  submitted: ReportMedia;
  source: ReportMedia | null;
  alignment: {
    status: string;
    matches: Array<{ match_id: string; clip_start_seconds: number; clip_end_seconds: number; source_start_seconds: number; source_end_seconds: number }>;
    match_quality: { confidence: Confidence; clip_coverage_ratio: number | null };
    edit_analysis: {
      status: "assessed" | "not_assessed";
      scope: string;
      counts: Record<string, number | null>;
      source_gaps: Array<{ start_seconds: number; end_seconds: number }>;
      unmatched_clip_ranges: Array<{ start_seconds: number; end_seconds: number }>;
    };
  } | null;
  context: { windows: Array<{ context_id: string; coverage: string; requested_start_seconds: number; requested_end_seconds: number; sentences: TranscriptSentence[] }> } | null;
  comparison: {
    context_assessment: string;
    caption_assessment: string;
    assessment_confidence: Confidence;
    summary: string;
    findings: Array<{ finding_id: string; type: string; explanation: string; evidence: Array<{ quote: string; start_seconds: number; end_seconds: number }> }>;
  } | null;
  summary: string | null;
  limitations: string[];
}

function apiBase(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
}

async function request(path: string, options?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${apiBase()}${path}`, { ...options, cache: "no-store" });
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

export async function startAnalysis(url: string, jobId: string): Promise<AnalysisResult> {
  const data = await request(`/analyze/${encodeURIComponent(jobId)}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
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
  const data = await request(`/reports/${encodeURIComponent(reportId)}`);
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

export function mediaUrl(media: ReportMedia["post"]["media"]): string | null {
  return publicUrl(media.playback_url) || (/^[A-Za-z0-9_-]{1,128}$/.test(media.media_id)
    ? `${apiBase()}/media/${encodeURIComponent(media.media_id)}` : null);
}
