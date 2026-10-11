import type {
  AnalysisResult, DisplayModel, ErrorCode, IngestResult, Outcome, PublicMediaAsset, ReportV2, Stage, TranscriptStatus,
} from "./api-types";
export type { AnalysisResult, ReportMedia, ReportV2 } from "./api-types";

const IDENTIFIER = /^[A-Za-z0-9_-]{1,128}$/;
const REPORT_ID = /^report_[A-Za-z0-9_-]{1,128}$/;

// Records keyed by the unions so the compiler flags any value added to the backend enums.
const ERROR_CODES: Record<ErrorCode, true> = {
  INVALID_URL: true, UNSUPPORTED_PLATFORM: true, MEDIA_UNAVAILABLE: true, TRANSCRIPTION_FAILED: true,
  SEARCH_FAILED: true, PROCESSING_FAILED: true, STORAGE_FAILED: true, MESSAGE_SEND_FAILED: true,
};
const STAGES: Record<Stage, true> = {
  retrieve_post: true, extract_media_features: true, build_search_queries: true, search_candidates: true,
  retrieve_candidate_media: true, verify_and_align: true, select_surrounding_context: true,
  compare_context_and_caption: true, build_report: true, save_report_and_complete_job: true,
  display_report: true, send_summary: true, run_analysis: true,
};
const OUTCOMES: Record<Outcome, true> = { matched: true, no_verified_match: true };
const DELIVERY_STATUSES: Record<AnalysisResult["delivery_status"], true> = { accepted: true, failed: true, skipped: true };
const TRANSCRIPT_STATUSES: Record<TranscriptStatus, true> = { complete: true, partial: true, unavailable: true };

/** An HTTP error from the API. `code` and `stage` are null when the body is not a QuantroError envelope. */
export class QuantroApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode | null;
  readonly stage: Stage | null;
  readonly retryable: boolean;

  constructor(message: string, status: number, code: ErrorCode | null, stage: Stage | null, retryable: boolean) {
    super(message);
    this.name = "QuantroApiError";
    this.status = status;
    this.code = code;
    this.stage = stage;
    this.retryable = retryable;
  }
}

function apiBase(): string {
  // Next.js substitutes this direct property access at build time.
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (configured) return configured.replace(/\/$/, "");
  if (typeof window !== "undefined" && !["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) {
    throw new Error("This deployment is missing its backend URL. Set NEXT_PUBLIC_API_BASE_URL and redeploy.");
  }
  return "http://localhost:8000";
}

function reportApiBase(): string {
  if (typeof window !== "undefined" && ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) return apiBase();
  return process.env.NEXT_PUBLIC_REPORT_API_BASE_URL?.trim().replace(/\/$/, "") || apiBase();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isMember<T extends string>(members: Record<T, true>, value: unknown): value is T {
  return typeof value === "string" && Object.hasOwn(members, value);
}

function toApiError(status: number, data: unknown): QuantroApiError {
  const body = isRecord(data) && isRecord(data.error) ? data.error : {};
  const message = typeof body.message === "string" ? body.message : `The request failed (${status}).`;
  return new QuantroApiError(
    message,
    status,
    isMember(ERROR_CODES, body.code) ? body.code : null,
    isMember(STAGES, body.stage) ? body.stage : null,
    body.retryable === true,
  );
}

async function request(path: string, options?: RequestInit, base = apiBase()): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { ...options, cache: "no-store" });
  } catch (error) {
    if (options?.signal?.aborted) throw error;
    throw new Error("Could not reach the analysis service. Check that the backend is running.");
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) throw toApiError(response.status, data);
  return data;
}

function postJson(path: string, body: unknown): Promise<unknown> {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function assertAnalysisResult(data: unknown, jobId: string): AnalysisResult {
  if (!isRecord(data) || data.job_id !== jobId || typeof data.report_id !== "string" || !REPORT_ID.test(data.report_id)
      || typeof data.report_url !== "string" || !isMember(OUTCOMES, data.outcome)
      || !isMember(DELIVERY_STATUSES, data.delivery_status)
      || !(data.delivery_warning === null || typeof data.delivery_warning === "string") || !Array.isArray(data.warnings)) {
    throw new Error("The analysis service returned an invalid result.");
  }
  return data as unknown as AnalysisResult;
}

function isRecordOrNull(value: unknown): boolean {
  return value === null || isRecord(value);
}

function assertReport(data: unknown): ReportV2 {
  if (!isRecord(data) || data.schema_version !== "2.1" || typeof data.job_id !== "string" || !isMember(OUTCOMES, data.outcome)
      || !isRecord(data.submitted) || !isRecord(data.submitted.post) || !isRecord(data.submitted.transcript)
      || !isRecordOrNull(data.source) || !isRecordOrNull(data.alignment) || !isRecordOrNull(data.context)
      || !isRecordOrNull(data.comparison) || !(data.summary === null || typeof data.summary === "string")
      || !Array.isArray(data.limitations)) {
    throw new Error("The analysis service returned an invalid report.");
  }
  return data as unknown as ReportV2;
}

function assertIngestResult(data: unknown, jobId: string): IngestResult {
  if (!isRecord(data) || data.job_id !== jobId || typeof data.url !== "string" || typeof data.post_id !== "string"
      || typeof data.media_id !== "string" || !isMember(TRANSCRIPT_STATUSES, data.transcript_status)
      || typeof data.sentence_count !== "number" || typeof data.frames_count !== "number"
      || typeof data.persisted_at !== "string") {
    throw new Error("The ingest service returned an invalid result.");
  }
  return data as unknown as IngestResult;
}

export async function runAnalysis(
  url: string, conversationId?: string, jobId = `job_${crypto.randomUUID()}`,
): Promise<AnalysisResult> {
  const body = conversationId === undefined ? { url } : { url, conversation_id: conversationId };
  return assertAnalysisResult(await postJson(`/analyze/${encodeURIComponent(jobId)}/run`, body), jobId);
}

export interface AnalysisInFlight {
  url: string;
  result: Promise<AnalysisResult>;
}

const inFlight = new Map<string, AnalysisInFlight>();

/** Starts the synchronous analysis and returns its job id so another page can await it after client-side navigation. */
export function startAnalysis(url: string): string {
  const jobId = `job_${crypto.randomUUID()}`;
  const result = runAnalysis(url, undefined, jobId);
  // The awaiting page reports the rejection; this only stops an unhandled-rejection report before it mounts.
  result.catch(() => undefined);
  inFlight.set(jobId, { url, result });
  return jobId;
}

/** Undefined after a full page load: the request lived in the previous page's memory. */
export function analysisInFlight(jobId: string): AnalysisInFlight | undefined {
  return inFlight.get(jobId);
}

export async function getReport(reportId: string): Promise<ReportV2> {
  if (!REPORT_ID.test(reportId)) throw new ReportNotFoundError();
  return assertReport(await requestReport(`/reports/${encodeURIComponent(reportId)}`));
}

export async function getDisplayModel(reportId: string): Promise<DisplayModel> {
  if (!REPORT_ID.test(reportId)) throw new ReportNotFoundError();
  const data = await requestReport(`/reports/${encodeURIComponent(reportId)}/display`);
  if (!isRecord(data) || typeof data.confidence_note !== "string" || !isRecord(data.submitted_panel)
      || !Array.isArray(data.limitations) || !isRecordOrNull(data.source_match)
      || !isRecordOrNull(data.clip_coverage) || !isRecordOrNull(data.context_assessment)) {
    throw new Error("The analysis service returned an invalid display model.");
  }
  return data as unknown as DisplayModel;
}

async function requestReport(path: string): Promise<unknown> {
  try {
    return await request(path, undefined, reportApiBase());
  } catch (error) {
    if (error instanceof QuantroApiError && error.status === 404) throw new ReportNotFoundError();
    throw error;
  }
}

export class ReportNotFoundError extends Error {
  readonly code = "REPORT_NOT_FOUND";
  readonly stage = "load_report";
  readonly retryable = false;

  constructor() {
    super("Report not found.");
    this.name = "ReportNotFoundError";
  }
}

export async function ingest(jobId: string, url: string): Promise<IngestResult> {
  if (!IDENTIFIER.test(jobId)) throw new Error("The job identifier is invalid.");
  return assertIngestResult(await postJson(`/ingest/${encodeURIComponent(jobId)}`, { url }), jobId);
}

export function publicUrl(value: string | null | undefined): string | null {
  if (!value || value.startsWith("//")) return null;
  try {
    const base = new URL(reportApiBase(), typeof window !== "undefined" ? window.location.origin : undefined);
    const url = new URL(value, base);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function mediaUrl(media: Pick<PublicMediaAsset, "media_id" | "playback_url">): string | null {
  return publicUrl(media.playback_url) || (IDENTIFIER.test(media.media_id)
    ? `${reportApiBase()}/media/${encodeURIComponent(media.media_id)}` : null);
}


/** Reconnect to persisted job state without starting another analysis. */
export async function waitForReport(jobId: string, signal?: AbortSignal): Promise<string> {
  if (!IDENTIFIER.test(jobId)) throw new Error("The job identifier is invalid.");
  const pause = () => new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException("Cancelled", "AbortError")); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, 2000);
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
  for (let attempt = 0; attempt < 450; attempt++) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    let data: unknown;
    try {
      data = await request(`/analyze/${encodeURIComponent(jobId)}/status`, { signal });
    } catch (error) {
      // The initial POST may still be registering its job when the page opens.
      if (error instanceof QuantroApiError && error.status === 404 && attempt < 5) { await pause(); continue; }
      throw error;
    }
    if (!isRecord(data) || data.job_id !== jobId) throw new Error("The service returned an invalid job status.");
    if (data.status === "completed") {
      if (typeof data.report_id !== "string" || !REPORT_ID.test(data.report_id)) throw new Error("The service returned an invalid report identifier.");
      return data.report_id;
    }
    if (data.status === "failed") throw toApiError(500, { error: data.error });
    if (typeof data.status !== "string" || !["pending", "running", "ingested"].includes(data.status)) throw new Error("The service returned an unknown job status.");
    await pause();
  }
  throw new Error("This analysis is taking longer than expected. Refresh to check its saved status again.");
}
