import { randomUUID } from "node:crypto";
import { mkdir, open, rename, unlink } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { Space } from "spectrum-ts";
import type { IntakeRecord } from "./receive.ts";
import { privatePathExists, readPrivateJson, RECOVERY_LIMITS, recoveryEntries, savedUrlIntake, withinRecoveryDeadline } from "./recovery.ts";

interface Options {
  directory: string;
  backendPort: number;
  request?: typeof fetch;
}

export interface RecoveryOptions {
  inboxDirectory: string;
  /** Explicit operator opt-in; the default performs GET-only reconciliation. */
  resumeUnstarted?: boolean;
  resolveConversation?: (record: IntakeRecord) => Promise<Space>;
}
export interface RecoverySummary {
  scanned: number;
  reconciled: number;
  resumed: number;
  unresolved: number;
  skipped: number;
  limited: boolean;
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

function newMarker(jobId: string): Marker {
  const now = new Date().toISOString();
  return { job_id: jobId, state: "pending", started_at: now, updated_at: now,
    http_status: null, error_code: null, error_stage: null, retryable: null,
    report_id: null, reconciled: false, backend_status: null };
}

async function persistMarker(path: string, marker: Marker): Promise<void> {
  marker.updated_at = new Date().toISOString();
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    const file = await open(temporary, "wx", 0o600);
    try { await file.writeFile(JSON.stringify(marker)); await file.sync(); }
    finally { await file.close(); }
    await rename(temporary, path);
  } finally { await unlink(temporary).catch(() => undefined); }
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
  const scheduled = new Set<string>();
  let recovery: Promise<RecoverySummary> | undefined;

  async function inspectStatus(path: string, deadline = Date.now() + 5_000): Promise<{ status: number; body?: Record<string, unknown> } | undefined> {
    const probeDeadline = Math.min(deadline, Date.now() + 5_000);
    try {
      return await withinRecoveryDeadline(async () => {
        const response = await request(`http://127.0.0.1:${options.backendPort}${path}`, {
          method: "GET", signal: AbortSignal.timeout(Math.max(1, probeDeadline - Date.now())), redirect: "error",
        });
        // Status probes must not allocate an unbounded report or wait forever on a partial body.
        const reader = response.body?.getReader();
        if (!reader) return { status: response.status };
        try {
          const chunks: Uint8Array[] = []; let length = 0;
          while (true) {
            const chunk = await withinRecoveryDeadline(() => reader.read(), probeDeadline);
            if (chunk.done) break;
            length += chunk.value.byteLength;
            if (length > 2 * 1024 * 1024) return { status: response.status };
            chunks.push(chunk.value);
          }
          return { status: response.status, body: object(JSON.parse(Buffer.concat(chunks).toString("utf8"))) };
        } finally { void reader.cancel().catch(() => undefined); reader.releaseLock(); }
      }, probeDeadline);
    } catch { return undefined; }
  }

  function completedReport(marker: Marker, saved: Record<string, unknown> | undefined): boolean {
    const reportId = `report_${marker.job_id}`;
    if (saved?.schema_version === "2.1" && saved.job_id === marker.job_id
        && (saved.outcome === "matched" || saved.outcome === "no_verified_match") && object(saved.submitted)) {
      marker.state = "completed";
      marker.report_id = reportId;
      marker.reconciled = true;
      marker.backend_status = "completed";
      marker.error_code = null; marker.error_stage = null; marker.retryable = null;
      return true;
    }
    return false;
  }

  function reconcileJob(marker: Marker, status: Record<string, unknown> | undefined): string {
    const job = object(status?.job);
    if (job?.id !== marker.job_id) return UNCERTAIN;
    if (typeof job.status === "string" && ["running", "ingested", "failed", "completed"].includes(job.status)) {
      marker.reconciled = true;
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

  async function reconcile(marker: Marker, deadline = Date.now() + 10_000): Promise<string> {
    const saved = await inspectStatus(`/reports/report_${marker.job_id}`, deadline);
    if (saved?.status === 200 && completedReport(marker, saved.body)) return COMPLETED_WITH_UNCONFIRMED_REPLY;
    const status = await inspectStatus(`/ingest/${marker.job_id}`, deadline);
    const notification = reconcileJob(marker, status?.status === 200 ? status.body : undefined);
    const error = object(saved?.body?.error);
    // A rejected stored report is a confirmed report failure, even if its database job says completed.
    // Storage/network failures still leave report validity unknown and must not become terminal failures.
    if (saved?.status === 500 && error?.code === "PROCESSING_FAILED"
        && error.stage === "display_report" && error.retryable === false) {
      marker.reconciled = true;
      marker.http_status = 500;
      markFailed(marker, { code: "PROCESSING_FAILED", stage: "display_report", retryable: false });
      return "The saved report did not pass validation. Ask the team to check the report before another analysis attempt.";
    }
    return notification;
  }

  async function run(record: IntakeRecord, eventKey: string, space: Space): Promise<void> {
    if (record.input_type !== "url" || !record.input_url || !/^[a-f0-9]{64}$/.test(eventKey)) return;
    const jobId = `photon_${eventKey}`;
    const path = join(directory, `${eventKey}.json`);
    const processing = newMarker(jobId);
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
        // A late completion/delivery error can follow a committed report. Saved evidence wins.
        const saved = await inspectStatus(`/reports/report_${jobId}`);
        notification = saved?.status === 200 && completedReport(processing, saved.body)
          ? COMPLETED_WITH_UNCONFIRMED_REPLY : markFailed(processing, confirmed);
      } else {
        if (result?.job_id !== jobId || result.report_id !== `report_${jobId}`
            || !["matched", "no_verified_match"].includes(typeof result.outcome === "string" ? result.outcome : "")
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
      try { await persistMarker(path, processing); }
      catch { /* Retaining the pending marker prevents an uncertain automatic replay. */ }
    }
    if (notification) await notify(space, notification);
  }

  function enqueue(record: IntakeRecord, eventKey: string, space: Space): boolean {
    if (scheduled.has(eventKey)) return false;
    if (queued >= 32) {
      void notify(space, "Your request is saved, but the analysis queue is full. Please wait and ask the team to process this saved request.");
      return false;
    }
    queued += 1;
    scheduled.add(eventKey);
    pending = pending.then(() => run(record, eventKey, space)).catch(() => undefined).finally(() => {
      queued -= 1; scheduled.delete(eventKey);
    });
    return true;
  }

  async function recoverSaved(settings: RecoveryOptions): Promise<RecoverySummary> {
    const result: RecoverySummary = { scanned: 0, reconciled: 0, resumed: 0, unresolved: 0, skipped: 0, limited: false };
    const deadline = Date.now() + RECOVERY_LIMITS.milliseconds;
    let jobs = 0;
    const room = (): boolean => {
      if (result.scanned >= RECOVERY_LIMITS.entries || jobs >= RECOVERY_LIMITS.jobs || Date.now() >= deadline) {
        result.limited = true; return false;
      }
      return true;
    };
    // Any existing marker prohibits automatic replay, including a legacy or malformed marker.
    for await (const entry of recoveryEntries(directory)) {
      if (!room()) break;
      result.scanned++;
      if (!entry.file || !/^[a-f0-9]{64}\.json$/.test(entry.name)) { result.skipped++; continue; }
      const eventKey = entry.name.slice(0, -5);
      if (scheduled.has(eventKey)) { result.skipped++; continue; }
      const path = join(directory, entry.name);
      try {
        const saved = object(await readPrivateJson(path, 8 * 1024));
        if (saved?.job_id !== `photon_${eventKey}` || typeof saved.state !== "string" || !["pending", "uncertain", "failed"].includes(saved.state)) {
          result.skipped++; continue;
        }
        jobs++;
        const marker = newMarker(`photon_${eventKey}`);
        marker.state = "uncertain";
        if (typeof saved.started_at === "string" && saved.started_at.endsWith("Z") && Number.isFinite(Date.parse(saved.started_at))) marker.started_at = saved.started_at;
        if (saved.state === "failed") {
          const error = failure(saved.error_code, saved.error_stage, saved.retryable);
          if (error) markFailed(marker, error);
        }
        if (Number.isInteger(saved.http_status) && Number(saved.http_status) >= 100 && Number(saved.http_status) <= 599) marker.http_status = Number(saved.http_status);
        await reconcile(marker, deadline);
        if (scheduled.has(eventKey)) { result.skipped++; continue; }
        await persistMarker(path, marker);
        if (marker.reconciled && ["completed", "failed"].includes(marker.state)) result.reconciled++;
        else result.unresolved++;
      } catch { result.unresolved++; }
    }
    for await (const entry of recoveryEntries(resolve(settings.inboxDirectory))) {
      if (!room()) break;
      result.scanned++;
      if (!entry.directory || !/^[a-f0-9]{64}$/.test(entry.name) || scheduled.has(entry.name)) { result.skipped++; continue; }
      const path = join(directory, `${entry.name}.json`);
      if (await privatePathExists(path)) { result.skipped++; continue; }
      try {
        const record = savedUrlIntake(await readPrivateJson(join(resolve(settings.inboxDirectory), entry.name, "request.json"), 64 * 1024), entry.name);
        if (!record) { result.skipped++; continue; }
        jobs++;
        const marker = newMarker(`photon_${entry.name}`);
        const report = await inspectStatus(`/reports/report_${marker.job_id}`, deadline);
        const status = await inspectStatus(`/ingest/${marker.job_id}`, deadline);
        // Both independent lookups must explicitly confirm absence before any new POST.
        const reportError = object(report?.body?.error);
        const jobError = object(status?.body?.error);
        const absent = report?.status === 404 && reportError?.message === "Report not found"
          && status?.status === 404 && jobError?.code === "MEDIA_UNAVAILABLE"
          && jobError.stage === "retrieve_post" && jobError.retryable === false && jobError.message === "Job not found";
        if (!absent || settings.resumeUnstarted !== true || !settings.resolveConversation) { result.unresolved++; continue; }
        const space = await withinRecoveryDeadline(() => settings.resolveConversation!(record), deadline);
        if (space.id !== record.conversation_id || scheduled.has(entry.name) || await privatePathExists(path) || queued >= 32) { result.skipped++; continue; }
        if (enqueue(record, entry.name, space)) result.resumed++; else result.skipped++;
      } catch { result.unresolved++; }
    }
    return result;
  }

  return {
    enqueue,
    recover(settings: RecoveryOptions): Promise<RecoverySummary> {
      recovery ??= recoverSaved(settings);
      return recovery;
    },
    idle(): Promise<void> { return pending; },
  };
}
