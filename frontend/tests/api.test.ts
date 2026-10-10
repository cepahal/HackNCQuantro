import assert from "node:assert/strict";
import test from "node:test";
import { getReport, mediaUrl, publicUrl, startAnalysis } from "../lib/api.ts";

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
