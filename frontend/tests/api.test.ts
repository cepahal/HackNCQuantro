import assert from "node:assert/strict";
import test from "node:test";
import {
  analysisInFlight, getDisplayModel, getReport, getReportDiagnostics, ingest, mediaUrl, publicUrl, QuantroApiError, ReportNotFoundError, runAnalysis, startAnalysis, waitForReport,
} from "../lib/api.ts";
import type { ReportV2 } from "../lib/api-types.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

const displayFixture = {
  source_match: null, clip_coverage: null, context_assessment: null,
  confidence_note: "Evidence is not a probability.", no_source_message: "No verified source found",
  context_unavailable_message: null, submitted_panel: {}, source_panel: null,
  poster_info: {submitted: {}, source: null}, matches: [], edit_analysis: null, findings: [], limitations: [],
};

test("getReport returns the typed ReportV2 from the report route", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(reportFixture));
  const report: ReportV2 = await getReport("report_job_1");
  assert.deepEqual(report, reportFixture);
  assert.equal(report.alignment?.match_quality.confidence.level, "moderate");
  assert.match(String(fetch.mock.calls[0]?.arguments[0]), /\/reports\/report_job_1$/);
});

test("diagnostics use the report endpoint and reject a different job or malformed event", async (t) => {
  const value = {job_id:"job_1",capture_status:"recorded",events:[{stage:"search_candidates",status:"completed",elapsed_seconds:1,data:{score:88}}],terminal_output:"Recorded output"};
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(value));
  assert.deepEqual(await getReportDiagnostics("report_job_1"), value);
  assert.match(String(fetch.mock.calls[0]?.arguments[0]), /\/reports\/report_job_1\/diagnostics$/);
  fetch.mock.mockImplementation(async () => Response.json({...value,job_id:"job_other"}));
  await assert.rejects(getReportDiagnostics("report_job_1"), /invalid diagnostics/);
  fetch.mock.mockImplementation(async () => Response.json({...value,events:[{stage:"x",status:"x",elapsed_seconds:-1,data:{}}]}));
  await assert.rejects(getReportDiagnostics("report_job_1"), /invalid diagnostics/);
  fetch.mock.mockImplementation(async () => Response.json({...value,events:[{stage:"x",status:"x",elapsed_seconds:1,timing_kind:"made_up",data:{}}]}));
  await assert.rejects(getReportDiagnostics("report_job_1"), /invalid diagnostics/);
});

test("a 400 error envelope throws QuantroApiError with its code and stage", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({
    error: { code: "INVALID_URL", stage: "retrieve_post", message: "The link could not be read as a valid URL.", retryable: false },
  }, { status: 400 }));
  await assert.rejects(runAnalysis("not a url"), (error: unknown) => {
    assert.ok(error instanceof QuantroApiError);
    assert.equal(error.status, 400);
    assert.equal(error.code, "INVALID_URL");
    assert.equal(error.stage, "retrieve_post");
    assert.equal(error.retryable, false);
    assert.equal(error.message, "The link could not be read as a valid URL.");
    return true;
  });
});

test("a report without schema_version is rejected", async (t) => {
  const withoutVersion: Partial<ReportV2> = { ...reportFixture };
  delete withoutVersion.schema_version;
  t.mock.method(globalThis, "fetch", async () => Response.json(withoutVersion));
  await assert.rejects(getReport("report_job_1"), /invalid report/);
});

test("a report missing a nullable top-level key is rejected", async (t) => {
  const withoutSource: Partial<ReportV2> = { ...reportFixture };
  delete withoutSource.source;
  t.mock.method(globalThis, "fetch", async () => Response.json(withoutSource));
  await assert.rejects(getReport("report_job_1"), /invalid report/);
});

test("runAnalysis posts the URL to a fresh job route and validates the echoed job", async (t) => {
  const calls: Array<{ url: string; body: unknown }> = [];
  t.mock.method(globalThis, "fetch", async (url: string, options?: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(options?.body)) });
    const jobId = /\/analyze\/([^/]+)\/run$/.exec(url)?.[1] ?? "";
    return Response.json({
      job_id: jobId, report_id: `report_${jobId}`, report_url: `http://localhost:3000/reports/report_${jobId}`,
      outcome: "no_verified_match", delivery_status: "skipped", delivery_warning: null, warnings: [],
    });
  });
  const result = await runAnalysis("https://example.com/post");
  assert.match(result.job_id, /^job_[0-9a-f-]{36}$/);
  assert.equal(result.report_id, `report_${result.job_id}`);
  assert.deepEqual(calls[0]?.body, { url: "https://example.com/post" });
  await runAnalysis("https://example.com/post", "chat_1");
  assert.deepEqual(calls[1]?.body, { url: "https://example.com/post", conversation_id: "chat_1" });
  assert.notEqual(calls[0]?.url, calls[1]?.url);
});

test("startAnalysis returns its job id immediately and keeps the pending result for the results page", async (t) => {
  t.mock.method(globalThis, "fetch", async (url: string) => {
    const jobId = /\/analyze\/([^/]+)\/run$/.exec(url)?.[1] ?? "";
    return Response.json({
      job_id: jobId, report_id: `report_${jobId}`, report_url: `http://localhost:3000/reports/report_${jobId}`,
      outcome: "matched", delivery_status: "skipped", delivery_warning: null, warnings: [],
    });
  });
  const jobId = startAnalysis("https://example.com/post");
  assert.match(jobId, /^job_[0-9a-f-]{36}$/);
  const run = analysisInFlight(jobId);
  assert.equal(run?.url, "https://example.com/post");
  assert.equal((await run!.result).report_id, `report_${jobId}`);
  assert.equal(analysisInFlight("job_unknown"), undefined);
});

test("a failed startAnalysis rejects only where it is awaited", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({
    error: { code: "SEARCH_FAILED", stage: "search_candidates", message: "Search is unavailable.", retryable: true },
  }, { status: 502 }));
  const run = analysisInFlight(startAnalysis("https://example.com/post"));
  await assert.rejects(run!.result, (error: unknown) => error instanceof QuantroApiError && error.code === "SEARCH_FAILED");
});

test("runAnalysis rejects a result for a different job", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({
    job_id: "someone_else", report_id: "report_someone_else", report_url: "http://localhost:3000/reports/report_someone_else",
    outcome: "matched", delivery_status: "skipped", delivery_warning: null, warnings: [],
  }));
  await assert.rejects(runAnalysis("https://example.com/post"), /invalid result/);
});

test("ingest posts to the ingest route and returns the typed IngestResult", async (t) => {
  const result = {
    job_id: "job_9", url: "https://example.com/post", post_id: "post_1", media_id: "media_1",
    transcript_status: "complete", sentence_count: 3, frames_count: 12, persisted_at: "2026-10-10T17:00:00+00:00",
  };
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(result));
  assert.deepEqual(await ingest("job_9", "https://example.com/post"), result);
  const [url, options] = fetch.mock.calls[0]?.arguments ?? [];
  assert.match(String(url), /\/ingest\/job_9$/);
  assert.deepEqual(JSON.parse(String((options as RequestInit).body)), { url: "https://example.com/post" });
});

test("a missing report is normalized to a non-retryable report-not-found error", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ error: { message: "Report not found" } }, { status: 404 }));
  await assert.rejects(getReport("report_missing"), (error: unknown) =>
    error instanceof ReportNotFoundError && error.code === "REPORT_NOT_FOUND" && !error.retryable && error.message === "Report not found.");
  await assert.rejects(getDisplayModel("report_missing"), (error: unknown) => error instanceof ReportNotFoundError);
});

test("connection errors hide raw transport details", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("private transport details"); });
  await assert.rejects(getReport("report_job_2"), (error: Error) => /Could not reach/.test(error.message) && !error.message.includes("private"));
});

test("invalid identifiers do not issue requests", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected fetch"); });
  await assert.rejects(getReport("../../private"), (error: unknown) => error instanceof ReportNotFoundError);
  await assert.rejects(ingest("../private", "https://example.com/post"), /identifier is invalid/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("media links use the byte-serving API and reject non-HTTP schemes", () => {
  assert.match(mediaUrl({ media_id: "media_1", playback_url: null }) ?? "", /\/media\/media_1$/);
  assert.equal(publicUrl("javascript:alert(1)"), null);
  assert.equal(publicUrl("//evil.example/x"), null);
  assert.equal(publicUrl("file:///private"), null);
  assert.equal(publicUrl("https://name:secret@example.com"), null);
  assert.equal(mediaUrl({ media_id: "../private", playback_url: null }), null);
});


test("refresh recovery reads a completed job without posting another analysis", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({
    job_id: "job_saved", status: "completed", report_id: "report_job_saved", error: null,
  }));
  assert.equal(await waitForReport("job_saved"), "report_job_saved");
  assert.match(String(fetch.mock.calls[0]?.arguments[0]), /analyze\/job_saved\/status$/);
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, undefined);
});
test("refresh recovery exposes the persisted safe failure", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({job_id: "job_saved", status: "failed", report_id: null,
    error: {code: "SEARCH_FAILED", stage: "search_candidates", message: "The source search failed.", retryable: false}}));
  await assert.rejects(waitForReport("job_saved"), (error: unknown) => error instanceof QuantroApiError && error.code === "SEARCH_FAILED");
});
test("refresh recovery cancels without fetching or restarting a job", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch");
  const controller = new AbortController(); controller.abort();
  await assert.rejects(waitForReport("job_saved", controller.signal), {name: "AbortError"});
  assert.equal(fetch.mock.callCount(), 0);
});


test("refresh recovery polls running jobs until the saved report exists", async (t) => {
  const original = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", ((callback: () => void) => original(callback, 0)) as typeof setTimeout);
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => Response.json(++calls === 1
    ? {job_id: "job_saved", status: "running", report_id: null, error: null}
    : {job_id: "job_saved", status: "completed", report_id: "report_job_saved", error: null}));
  assert.equal(await waitForReport("job_saved"), "report_job_saved");
  assert.equal(calls, 2);
});

test("confidence labels use the backend display endpoint and reject malformed data", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(displayFixture));
  assert.equal((await getDisplayModel("report_job_1")).confidence_note, displayFixture.confidence_note);
  assert.match(String(fetch.mock.calls[0]?.arguments[0]), /\/reports\/report_job_1\/display$/);
  fetch.mock.mockImplementation(async () => Response.json({}));
  await assert.rejects(getDisplayModel("report_job_1"), /invalid display model/);
});

test("report sharing keeps analysis local and uses the public report API only off loopback", async (t) => {
  const api = process.env.NEXT_PUBLIC_API_BASE_URL;
  const reports = process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
  const window = Object.getOwnPropertyDescriptor(globalThis, "window");
  t.after(() => {
    if (api === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL; else process.env.NEXT_PUBLIC_API_BASE_URL = api;
    if (reports === undefined) delete process.env.NEXT_PUBLIC_REPORT_API_BASE_URL; else process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = reports;
    if (window) Object.defineProperty(globalThis, "window", window); else Reflect.deleteProperty(globalThis, "window");
  });
  process.env.NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000";
  process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = "https://reports.example/api/";
  const urls: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string) => {
    urls.push(url);
    if (url.endsWith("/run")) return Response.json({job_id:"job_1",report_id:"report_job_1",
      report_url:"http://localhost:3000/reports/report_job_1",outcome:"matched",delivery_status:"skipped",delivery_warning:null,warnings:[]});
    return Response.json(url.endsWith("/display") ? displayFixture : reportFixture);
  });
  await runAnalysis("https://example.com/video.mp4", undefined, "job_1");
  await getReport("report_job_1");
  await getDisplayModel("report_job_1");
  assert.deepEqual(urls, ["http://localhost:8000/analyze/job_1/run", "https://reports.example/api/reports/report_job_1",
    "https://reports.example/api/reports/report_job_1/display"]);
  assert.equal(mediaUrl({media_id:"media_1",playback_url:null}), "https://reports.example/api/media/media_1");
  for (const hostname of ["localhost", "127.0.0.1", "[::1]"]) {
    Object.defineProperty(globalThis, "window", {configurable:true,value:{location:{hostname}}});
    await getReport("report_job_1");
    assert.equal(urls.at(-1), "http://localhost:8000/reports/report_job_1");
    assert.equal(mediaUrl({media_id:"media_1",playback_url:null}), "http://localhost:8000/media/media_1");
  }
});

test("public deployment without a backend URL fails before fetching visitor localhost", async (t) => {
  const api = process.env.NEXT_PUBLIC_API_BASE_URL;
  const reports = process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
  const window = Object.getOwnPropertyDescriptor(globalThis, "window");
  t.after(() => {
    if (api === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL; else process.env.NEXT_PUBLIC_API_BASE_URL = api;
    if (reports === undefined) delete process.env.NEXT_PUBLIC_REPORT_API_BASE_URL; else process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = reports;
    if (window) Object.defineProperty(globalThis, "window", window); else Reflect.deleteProperty(globalThis, "window");
  });
  delete process.env.NEXT_PUBLIC_API_BASE_URL;
  delete process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
  Object.defineProperty(globalThis, "window", {configurable:true,value:{location:{hostname:"quantro-ten.vercel.app"}}});
  const fetch = t.mock.method(globalThis, "fetch");
  await assert.rejects(runAnalysis("https://example.com/video.mp4"), /NEXT_PUBLIC_API_BASE_URL and redeploy/);
  await assert.rejects(getReport("report_job_1"), /NEXT_PUBLIC_API_BASE_URL and redeploy/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("same-origin report API preserves original and playback URLs safely", (t) => {
  const reports = process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
  const window = Object.getOwnPropertyDescriptor(globalThis, "window");
  t.after(() => {
    if (reports === undefined) delete process.env.NEXT_PUBLIC_REPORT_API_BASE_URL; else process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = reports;
    if (window) Object.defineProperty(globalThis, "window", window); else Reflect.deleteProperty(globalThis, "window");
  });
  process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = "/api";
  Object.defineProperty(globalThis, "window", {configurable:true,value:{location:{hostname:"reports.example",origin:"https://reports.example"}}});
  assert.equal(publicUrl("https://www.instagram.com/reel/example/"), "https://www.instagram.com/reel/example/");
  assert.equal(publicUrl("/api/media/media_1"), "https://reports.example/api/media/media_1");
  assert.equal(mediaUrl({media_id:"media_1",playback_url:"https://media.example/video.mp4"}), "https://media.example/video.mp4");
  assert.equal(mediaUrl({media_id:"media_1",playback_url:null}), "/api/media/media_1");
  assert.equal(publicUrl("javascript:alert(1)"), null);
  assert.equal(publicUrl("https://name:secret@example.com/video.mp4"), null);
});

test("refresh recovery rejects an array status instead of coercing it", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({job_id:"job_saved",status:["running"]}));
  await assert.rejects(waitForReport("job_saved"), /unknown job status/);
});

test("saved-report polling rejects a mismatched job ID and unknown status", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => Response.json(++calls === 1
    ? { job_id: "job_other", status: "running", report_id: null, error: null }
    : { job_id: "job_saved", status: "mystery", report_id: null, error: null }));
  await assert.rejects(waitForReport("job_saved"), /invalid job status/);
  await assert.rejects(waitForReport("job_saved"), /unknown job status/);
});

test("saved-report polling rejects completed jobs without a usable report ID", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ job_id: "job_saved", status: "completed", report_id: null, error: null }));
  await assert.rejects(waitForReport("job_saved"), /invalid report identifier/);
});

test("saved-report polling reports a bounded timeout", async (t) => {
  t.mock.method(globalThis, "setTimeout", ((callback: () => void) => { queueMicrotask(callback); return 0; }) as unknown as typeof setTimeout);
  t.mock.method(globalThis, "fetch", async () => Response.json({ job_id: "job_saved", status: "running", report_id: null, error: null }));
  await assert.rejects(waitForReport("job_saved"), /taking longer than expected/);
});
