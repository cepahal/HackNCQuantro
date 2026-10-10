import { mkdir, open, rename, unlink } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { Space } from "spectrum-ts";
import type { IntakeRecord } from "./receive.ts";

interface Options {
  directory: string;
  backendPort: number;
  request?: typeof fetch;
}

const FAILURE_MESSAGES = {
  INVALID_URL: "The submitted link is invalid. Send a valid public post URL.",
  UNSUPPORTED_PLATFORM: "This post platform is unsupported. Send a YouTube or direct video URL.",
  MEDIA_UNAVAILABLE: "The media could not be retrieved. Check that the post is public and accessible.",
  TRANSCRIPTION_FAILED: "The audio could not be transcribed. Ask the team to check transcription.",
  SEARCH_FAILED: "Source search failed. Ask the team to check the search provider.",
  PROCESSING_FAILED: "Analysis stopped during processing. Ask the team to check the failed stage.",
  STORAGE_FAILED: "Analysis could not safely save its result. Ask the team to check storage.",
  MESSAGE_SEND_FAILED: "The report reply failed. Ask the team to check messaging before retrying.",
} as const;
const STAGES = new Set([
  "retrieve_post", "extract_media_features", "build_search_queries", "search_candidates",
  "retrieve_candidate_media", "verify_and_align", "select_surrounding_context",
  "compare_context_and_caption", "build_report", "save_report_and_complete_job",
  "display_report", "send_summary", "run_analysis",
]);
type FailureCode = keyof typeof FAILURE_MESSAGES;
interface Failure { code: FailureCode; stage: string; retryable: boolean | null }
interface Marker {
  job_id: string;
  state: "pending" | "completed" | "failed" | "uncertain";
  started_at: string;
  updated_at: string;
  http_status: number | null;
  error_code: FailureCode | null;
  error_stage: string | null;
  retryable: boolean | null;
  report_id: string | null;
  reconciled: boolean;
  backend_status: string | null;
}
const COMPLETED_WITH_UNCONFIRMED_REPLY = "Your analysis is complete, but the report reply could not be confirmed. Your report remains saved; ask the team to retrieve it.";
const UNCERTAIN = "I could not confirm completion of this analysis. Your request is saved. The team can check its status before retrying.";
export const BACKEND_NOT_READY_MESSAGE = "The backend is not ready. Start it and resolve its startup checks before starting Photon.";

export async function assertBackendReady(backendPort: number, request: typeof fetch = fetch): Promise<void> {
  try {
    if (!Number.isInteger(backendPort) || backendPort < 1024 || backendPort > 65535) throw new Error();
    const response = await request(`http://127.0.0.1:${backendPort}/openapi.json`, {
      method: "GET", signal: AbortSignal.timeout(5_000), redirect: "error",
    });
    if (!response.ok) { await response.body?.cancel(); throw new Error(); }
    const specification = object(await response.json());
    const paths = object(specification?.paths);
    for (const [path, method] of [["/analyze/{job_id}/run", "post"], ["/reports/{report_id}", "get"],
      ["/ingest/{job_id}", "post"], ["/ingest/{job_id}", "get"]] as const) {
      if (!object(object(paths?.[path])?.[method])) throw new Error();
    }
  } catch {
    throw new Error(BACKEND_NOT_READY_MESSAGE);
  }
}

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function failure(code: unknown, stage: unknown, retryable: unknown): Failure | undefined {
  if (typeof code !== "string" || !Object.hasOwn(FAILURE_MESSAGES, code)
      || typeof stage !== "string" || !STAGES.has(stage)
      || !(retryable === null || typeof retryable === "boolean")) return undefined;
  return { code: code as FailureCode, stage, retryable };
}

function markFailed(marker: Marker, error: Failure): string {
  marker.state = "failed";
  marker.error_code = error.code;
  marker.error_stage = error.stage;
  marker.retryable = error.retryable;
  return `${FAILURE_MESSAGES[error.code]} Your request is saved; the team can check its status before another analysis attempt.`;
}

async function notify(space: Space, text: string): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([space.send(text), new Promise<void>((resolve) => { timer = setTimeout(resolve, 12_000); })]);
  } catch { /* An acknowledgement failure never changes persisted processing state. */ }
  finally { if (timer) clearTimeout(timer); }
}

/** Serialize URL investigations; durable markers prohibit accidental automatic replay. */
export function createAnalysisDispatcher(options: Options) {
  if (!Number.isInteger(options.backendPort) || options.backendPort < 1024 || options.backendPort > 65535) {
    throw new Error("Use a valid local backend port.");
  }
  const directory = resolve(options.directory);
  const request = options.request ?? fetch;
  let pending = Promise.resolve();
  let queued = 0;

  async function readStatus(path: string): Promise<Record<string, unknown> | undefined> {
    try {
      const response = await request(`http://127.0.0.1:${options.backendPort}${path}`, {
        method: "GET", signal: AbortSignal.timeout(5_000), redirect: "error",
      });
      if (!response.ok) { await response.body?.cancel(); return undefined; }
      return object(await response.json());
    } catch { return undefined; }
  }

  async function reconcile(marker: Marker): Promise<string> {
    const reportId = `report_${marker.job_id}`;
    const saved = await readStatus(`/reports/${reportId}`);
    if (saved?.schema_version === "2.1" && saved.job_id === marker.job_id
        && (saved.outcome === "matched" || saved.outcome === "no_verified_match") && object(saved.submitted)) {
      marker.state = "completed";
      marker.report_id = reportId;
      marker.reconciled = true;
      marker.backend_status = "completed";
      return COMPLETED_WITH_UNCONFIRMED_REPLY;
    }
    const status = await readStatus(`/ingest/${marker.job_id}`);
    const job = object(status?.job);
    if (job?.id !== marker.job_id) return UNCERTAIN;
    marker.reconciled = true;
    if (typeof job.status === "string" && ["running", "ingested", "failed", "completed"].includes(job.status)) {
      marker.backend_status = job.status;
    }
    if (job.status === "failed") {
      const error = failure(job.error_code, job.error_stage, null);
      if (error) return markFailed(marker, error);
    }
    if (job.status === "running" || job.status === "ingested") {
      return "Your analysis is still in progress, but I lost its live response. Your request is saved; the team can check its status before retrying.";
    }
    return UNCERTAIN;
  }

  async function run(record: IntakeRecord, eventKey: string, space: Space): Promise<void> {
    if (record.input_type !== "url" || !record.input_url || !/^[a-f0-9]{64}$/.test(eventKey)) return;
    const jobId = `photon_${eventKey}`;
    const path = join(directory, `${eventKey}.json`);
    const now = new Date().toISOString();
    const processing: Marker = {
      job_id: jobId, state: "pending", started_at: now, updated_at: now,
      http_status: null, error_code: null, error_stage: null, retryable: null,
      report_id: null, reconciled: false, backend_status: null,
    };
    try {
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const marker = await open(path, "wx", 0o600);
      try { await marker.writeFile(JSON.stringify(processing)); await marker.sync(); }
      finally { await marker.close(); }
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "EEXIST") return;
      await notify(space, "Your request is saved, but analysis could not start because its processing state could not be stored.");
      return;
    }
    processing.state = "uncertain";
    let notification: string | undefined;
    try {
      const response = await request(`http://127.0.0.1:${options.backendPort}/analyze/${jobId}/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: record.input_url, conversation_id: record.conversation_id }),
        signal: AbortSignal.timeout(30 * 60 * 1000), redirect: "error",
      });
      processing.http_status = response.status;
      const result = object(await response.json());
      if (!response.ok) {
        const error = object(result?.error);
        const confirmed = error && typeof error.retryable === "boolean" && failure(error.code, error.stage, error.retryable);
        if (!confirmed) throw new Error("Analysis response was not a recognized failure");
        notification = markFailed(processing, confirmed);
      } else {
        if (result?.job_id !== jobId || result.report_id !== `report_${jobId}`
            || typeof result.delivery_status !== "string"
            || !["accepted", "failed", "skipped"].includes(result.delivery_status)) {
          throw new Error("Analysis response did not confirm this job");
        }
        processing.state = "completed";
        processing.report_id = `report_${jobId}`;
        if (result.delivery_status !== "accepted") notification = COMPLETED_WITH_UNCONFIRMED_REPLY;
      }
    } catch {
      notification = await reconcile(processing);
    } finally {
      processing.updated_at = new Date().toISOString();
      const temporary = `${path}.tmp`;
      try {
        const marker = await open(temporary, "wx", 0o600);
        try { await marker.writeFile(JSON.stringify(processing)); await marker.sync(); }
        finally { await marker.close(); }
        await rename(temporary, path);
      } catch { /* Retaining the pending marker prevents an uncertain automatic replay. */ }
      finally { await unlink(temporary).catch(() => undefined); }
    }
    if (notification) await notify(space, notification);
  }

  return {
    enqueue(record: IntakeRecord, eventKey: string, space: Space): void {
      if (queued >= 32) {
        void notify(space, "Your request is saved, but the analysis queue is full. Please wait and ask the team to process this saved request.");
        return;
      }
      queued += 1;
      pending = pending.then(() => run(record, eventKey, space)).catch(() => undefined).finally(() => { queued -= 1; });
    },
    idle(): Promise<void> { return pending; },
  };
}
