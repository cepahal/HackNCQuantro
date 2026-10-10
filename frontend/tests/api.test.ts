import assert from "node:assert/strict";
import test from "node:test";
import { getReport, getDisplayModel, mediaUrl, publicUrl, runAnalysis, startAnalysis } from "../lib/api.ts";

test("submission sends the exact route and JSON contract, then fetches the persisted report", async (t) => {
  const calls: Array<{ url: string; options: RequestInit | undefined }> = [];
  t.mock.method(globalThis, "fetch", async (url: string, options?: RequestInit) => {
    calls.push({ url, options });
    return Response.json(calls.length === 1 ? {
      job_id: "job_1", outcome: "no_verified_match", report_id: "report_job_1", report_url: "http://localhost:3000/reports/report_job_1",
      delivery_status: "skipped", delivery_warning: null, warnings: [],
    } : { schema_version: "2.1", job_id: "job_1", outcome: "no_verified_match", submitted: {}, limitations: [] });
  });
  const result = await startAnalysis("https://example.com/post", "job_1");
  assert.equal(result.report_id, "report_job_1");
  assert.deepEqual(JSON.parse(String(calls[0]?.options?.body)), { url: "https://example.com/post" });
  assert.match(calls[0]?.url ?? "", /\/analyze\/job_1\/run$/);
  await getReport(result.report_id);
  assert.match(calls[1]?.url ?? "", /\/reports\/report_job_1$/);
});

test("backend error envelopes show their safe message", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ error: { message: "The post is unavailable." } }, { status: 404 }));
  await assert.rejects(startAnalysis("https://example.com/missing", "job_2"), /The post is unavailable/);
});

test("connection errors hide raw transport details", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("private transport details"); });
  await assert.rejects(getReport("report_job_2"), (error: Error) => /Could not reach/.test(error.message) && !error.message.includes("private"));
});

test("invalid report identifiers do not issue requests", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected fetch"); });
  await assert.rejects(getReport("../../private"), /identifier is invalid/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("malformed stage responses cannot be rendered as successful reports", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ outcome: "matched" }));
  await assert.rejects(startAnalysis("https://example.com/post", "job_1"), /invalid result/);
  await assert.rejects(getReport("report_job_1"), /invalid report/);
});

test("media links use the byte-serving API and reject non-HTTP schemes", () => {
  assert.match(mediaUrl({ media_id: "media_1", playback_url: null, mime_type: "video/mp4" }) ?? "", /\/media\/media_1$/);
  assert.equal(publicUrl("javascript:alert(1)"), null);
  assert.equal(publicUrl("file:///private"), null);
  assert.equal(publicUrl("https://name:secret@example.com"), null);
  assert.equal(mediaUrl({ media_id: "../private", playback_url: null, mime_type: "video/mp4" }), null);
});


test("runAnalysis generates the required job ID and forwards conversation ID", async (t) => {
  let sent: Record<string, unknown> = {};
  t.mock.method(globalThis, "fetch", async (url: string, options?: RequestInit) => {
    const jobId = new URL(url).pathname.split("/")[2]!;
    assert.match(jobId, /^job_[0-9a-f-]{36}$/);
    sent = JSON.parse(String(options?.body));
    return Response.json({job_id:jobId, outcome:"no_verified_match", report_id:`report_${jobId}`,
      report_url:`http://localhost:3000/reports/report_${jobId}`, delivery_status:"skipped", delivery_warning:null, warnings:[]});
  });
  await runAnalysis("https://example.com/video.mp4", "conv_test");
  assert.deepEqual(sent, {url:"https://example.com/video.mp4", conversation_id:"conv_test"});
});

test("confidence display data comes from the backend DisplayModel endpoint", async (t) => {
  t.mock.method(globalThis, "fetch", async (url: string) => {
    assert.match(url, /\/reports\/report_job_1\/display$/);
    return Response.json({confidence_note:"Evidence is not a probability.", submitted_panel:{}, limitations:[]});
  });
  assert.equal((await getDisplayModel("report_job_1")).confidence_note, "Evidence is not a probability.");
});

test("malformed display data fails safely", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({}));
  await assert.rejects(getDisplayModel("report_job_1"), /invalid display model/);
});

test("read-only report sharing preserves the separate analysis endpoint", async (t) => {
  const priorApi = process.env.NEXT_PUBLIC_API_BASE_URL;
  const priorReportApi = process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
  t.after(() => {
    if (priorApi === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
    else process.env.NEXT_PUBLIC_API_BASE_URL = priorApi;
    if (priorReportApi === undefined) delete process.env.NEXT_PUBLIC_REPORT_API_BASE_URL;
    else process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = priorReportApi;
  });
  process.env.NEXT_PUBLIC_API_BASE_URL = "http://localhost:8000";
  process.env.NEXT_PUBLIC_REPORT_API_BASE_URL = "https://reports.example/api/";
  const urls: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string) => {
    urls.push(url);
    if (url.endsWith("/run")) return Response.json({ job_id: "job_1", outcome: "no_verified_match",
      report_id: "report_job_1", warnings: [] });
    if (!url.endsWith("/display")) return Response.json({ schema_version: "2.1", submitted: {},
      outcome: "no_verified_match", limitations: [] });
    return Response.json({ confidence_note: "Evidence is not a probability.", submitted_panel: {}, limitations: [] });
  });
  await startAnalysis("https://example.com/video.mp4", "job_1");
  await getReport("report_job_1");
  await getDisplayModel("report_job_1");
  assert.deepEqual(urls, ["http://localhost:8000/analyze/job_1/run",
    "https://reports.example/api/reports/report_job_1", "https://reports.example/api/reports/report_job_1/display"]);
  assert.equal(mediaUrl({ media_id: "media_1", playback_url: null, mime_type: "video/mp4" }),
    "https://reports.example/api/media/media_1");

  const priorWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  t.after(() => {
    if (priorWindow) Object.defineProperty(globalThis, "window", priorWindow);
    else Reflect.deleteProperty(globalThis, "window");
  });
  for (const hostname of ["localhost", "127.0.0.1", "[::1]"]) {
    Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { hostname } } });
    await getReport("report_job_1");
    await getDisplayModel("report_job_1");
    assert.deepEqual(urls.slice(-2), ["http://localhost:8000/reports/report_job_1",
      "http://localhost:8000/reports/report_job_1/display"]);
    assert.equal(mediaUrl({ media_id: "media_1", playback_url: null, mime_type: "video/mp4" }),
      "http://localhost:8000/media/media_1");
  }
  Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { hostname: "reports.example" } } });
  await getDisplayModel("report_job_1");
  assert.equal(urls.at(-1), "https://reports.example/api/reports/report_job_1/display");
});
