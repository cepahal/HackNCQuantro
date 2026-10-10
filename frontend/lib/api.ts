import { fixtureReport } from "./fixtures";
import { parseReport } from "./schema";
import type { ApiError, Job, Report } from "./types";

const base = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const mock = process.env.NEXT_PUBLIC_USE_MOCK !== "false";
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const mockJobs = new Map<string, Job>();
const stages = ["retrieving", "transcribing", "searching", "aligning", "building_report"];
export function normalizeError(value: unknown, fallback = "The request could not be completed."): ApiError {
  if (typeof value === "object" && value !== null && "error" in value) {
    const error = (value as {error: Partial<ApiError["error"]>}).error;
    return {error:{code:error.code || "REQUEST_FAILED",stage:error.stage || "unknown",message:error.message || fallback,retryable:error.retryable ?? true}};
  }
  return {error:{code:"REQUEST_FAILED",stage:"unknown",message:value instanceof Error ? value.message : fallback,retryable:true}};
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`${base}${path}`, init); }
  catch (error) { throw normalizeError(error, "Could not reach the Unzoom service."); }
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw normalizeError(payload, `Request failed (${response.status}).`);
  return payload as T;
}
export async function createJob(input: { file?: File; url?: string }): Promise<{job_id:string}> {
  if (mock) { const job_id = `job_${crypto.randomUUID().slice(0,8)}`; mockJobs.set(job_id,{job_id,status:"queued"}); return {job_id}; }
  if (input.file) {
    const form = new FormData(); form.append("file", input.file);
    return request("/api/jobs",{method:"POST",body:form});
  }
  return request("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:input.url})});
}
export async function getJob(id: string): Promise<Job> {
  if (mock) {
    await delay(800);
    const current = mockJobs.get(id) || {job_id:id,status:"queued" as const};
    const index = current.stage ? Math.min(stages.indexOf(current.stage)+1,stages.length-1) : 0;
    const status = current.stage === stages.at(-1) ? "completed" : "running";
    const next: Job = {job_id:id,status,stage:stages[index],...(status==="completed"?{report_id:"matched"}:{})};
    mockJobs.set(id,next); return next;
  }
  return request(`/api/jobs/${encodeURIComponent(id)}`);
}
export async function getReport(id: string): Promise<Report> {
  const value: unknown = mock ? (await delay(450), fixtureReport(id === "no_match" ? "no_match" : id === "context_unavailable" ? "context_unavailable" : "matched")) : await request(`/api/reports/${encodeURIComponent(id)}`);
  const parsed = parseReport(value);
  if (!parsed.success) {
    if (process.env.NODE_ENV === "development") console.error("Report format mismatch", parsed.issues);
    throw normalizeError({error:{code:"REPORT_FORMAT_MISMATCH",stage:"validate_report",message:"This report could not be displayed because its format is unexpected.",retryable:false}});
  }
  if (parsed.data.schema_version !== "2.1" && process.env.NODE_ENV === "development") console.warn(`Unexpected report schema version: ${parsed.data.schema_version}`);
  return parsed.data;
}
