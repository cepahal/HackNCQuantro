import assert from "node:assert/strict";
import test from "node:test";
import { getReport, ingest, mediaUrl, publicUrl, QuantroApiError, runAnalysis } from "../lib/api.ts";
import type { ReportV2 } from "../lib/api-types.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

test("getReport returns the typed ReportV2 from the report route", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(reportFixture));
  const report: ReportV2 = await getReport("report_job_1");
  assert.deepEqual(report, reportFixture);
  assert.equal(report.alignment?.match_quality.confidence.level, "moderate");
  assert.match(String(fetch.mock.calls[0]?.arguments[0]), /\/reports\/report_job_1$/);
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
  const { schema_version: _omitted, ...withoutVersion } = reportFixture;
  t.mock.method(globalThis, "fetch", async () => Response.json(withoutVersion));
  await assert.rejects(getReport("report_job_1"), /invalid report/);
});

test("a report missing a nullable top-level key is rejected", async (t) => {
  const { source: _omitted, ...withoutSource } = reportFixture;
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

test("non-envelope errors still throw QuantroApiError without a code", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ error: { message: "Report not found" } }, { status: 404 }));
  await assert.rejects(getReport("report_missing"), (error: unknown) =>
    error instanceof QuantroApiError && error.status === 404 && error.code === null && error.message === "Report not found");
});

test("connection errors hide raw transport details", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("private transport details"); });
  await assert.rejects(getReport("report_job_2"), (error: Error) => /Could not reach/.test(error.message) && !error.message.includes("private"));
});

test("invalid identifiers do not issue requests", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected fetch"); });
  await assert.rejects(getReport("../../private"), /identifier is invalid/);
  await assert.rejects(ingest("../private", "https://example.com/post"), /identifier is invalid/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("media links use the byte-serving API and reject non-HTTP schemes", () => {
  assert.match(mediaUrl({ media_id: "media_1", playback_url: null }) ?? "", /\/media\/media_1$/);
  assert.equal(publicUrl("javascript:alert(1)"), null);
  assert.equal(publicUrl("file:///private"), null);
  assert.equal(publicUrl("https://name:secret@example.com"), null);
  assert.equal(mediaUrl({ media_id: "../private", playback_url: null }), null);
});
