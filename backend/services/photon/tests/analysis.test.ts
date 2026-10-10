import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { Space } from "spectrum-ts";
import { assertBackendReady, BACKEND_NOT_READY_MESSAGE, createAnalysisDispatcher } from "../src/analysis.ts";
import type { IntakeRecord } from "../src/receive.ts";

const event = "a".repeat(64);
const record: IntakeRecord = {
  schema_version: "photon-intake-1", conversation_id: "real-conversation-guid", message_id: "message_1",
  receiving_line: null, input_type: "url", input_url: "https://example.com/post", text: "Check this post", attachments: [], status: "received", received_at: "2026-10-10T17:00:00Z",
};
function chat() {
  const sent: string[] = [];
  return { sent, space: { async send(text: string) { sent.push(text); } } as unknown as Space };
}
async function directory(t: { after(fn: () => Promise<void>): void }) {
  const path = await mkdtemp(join(tmpdir(), "quantro-dispatch-"));
  t.after(() => rm(path, { force: true, recursive: true }));
  return path;
}

test("URL intake calls the real analysis route after a durable marker and preserves conversation", async (t) => {
  const path = await directory(t);
  const channel = chat();
  const calls: Array<{ url: string; body: unknown }> = [];
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (url, options) => {
    const marker = JSON.parse(await readFile(join(path, `${event}.json`), "utf8"));
    assert.equal(marker.state, "pending");
    calls.push({ url: String(url), body: JSON.parse(String(options?.body)) });
    return Response.json({ job_id: `photon_${event}`, report_id: `report_photon_${event}`, outcome: "matched", delivery_status: "accepted" });
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space);
  await dispatcher.idle();
  assert.equal(calls[0]?.url, `http://127.0.0.1:8000/analyze/photon_${event}/run`);
  assert.deepEqual(calls[0]?.body, { url: record.input_url, conversation_id: record.conversation_id });
  assert.equal(JSON.parse(await readFile(join(path, `${event}.json`), "utf8")).state, "completed");
  assert.deepEqual(channel.sent, []);
  dispatcher.enqueue(record, event, channel.space);
  await dispatcher.idle();
  assert.equal(calls.length, 1);
});

test("analysis errors are acknowledged safely and never automatically retried", async (t) => {
  const path = await directory(t); const channel = chat(); let calls = 0;
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (_url, options) => {
    if (options?.method === "POST") calls++;
    throw new Error("private provider credential details");
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  assert.match(channel.sent[0] ?? "", /could not confirm completion/);
  assert.ok(!channel.sent.join().includes("private"));
  assert.equal(JSON.parse(await readFile(join(path, `${event}.json`), "utf8")).state, "uncertain");
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle(); assert.equal(calls, 1);
});

test("successful analysis with failed delivery preserves completion", async (t) => {
  const path = await directory(t); const channel = chat();
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async () => Response.json({
    job_id: `photon_${event}`, report_id: `report_photon_${event}`, outcome: "matched", delivery_status: "failed",
  })) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  assert.match(channel.sent[0] ?? "", /report remains saved/);
  assert.equal(JSON.parse(await readFile(join(path, `${event}.json`), "utf8")).state, "completed");
});

test("attachments are not submitted as invented public URLs", async (t) => {
  const path = await directory(t); let calls = 0;
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async () => { calls++; return Response.json({}); }) as typeof fetch });
  dispatcher.enqueue({ ...record, input_type: "attachment", input_url: null }, event, chat().space);
  await dispatcher.idle(); assert.equal(calls, 0);
});

test("rejects invalid backend ports", () => {
  for (const backendPort of [0, 80, 65536, 8000.5]) assert.throws(() => createAnalysisDispatcher({ directory: ".", backendPort }));
});

for (const [httpStatus, code, stage] of [
  [400, "INVALID_URL", "retrieve_post"],
  [404, "MEDIA_UNAVAILABLE", "retrieve_post"],
  [500, "PROCESSING_FAILED", "retrieve_post"],
] as const) {
  test(`structured HTTP ${httpStatus} failure is persisted and never retried or mislabeled uncertain`, async (t) => {
    const path = await directory(t); const channel = chat(); let posts = 0;
    const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (_url, options) => {
      if (options?.method === "GET") return Response.json({}, { status: 404 });
      assert.equal(options?.method, "POST");
      posts++;
      return Response.json({ error: { code, stage, retryable: false,
        message: "PRIVATE_SECRET file:///private/.env traceback password=do-not-echo" } }, { status: httpStatus });
    }) as typeof fetch });
    dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
    const raw = await readFile(join(path, `${event}.json`), "utf8");
    const marker = JSON.parse(raw);
    assert.equal(marker.state, "failed");
    assert.equal(marker.error_code, code);
    assert.equal(marker.error_stage, stage);
    assert.equal(marker.retryable, false);
    assert.equal(marker.http_status, httpStatus);
    assert.match(marker.started_at, /^\d{4}-\d{2}-\d{2}T.*Z$/);
    assert.match(marker.updated_at, /^\d{4}-\d{2}-\d{2}T.*Z$/);
    assert.ok(!channel.sent.join().includes("could not confirm completion"));
    assert.ok(![raw, ...channel.sent].join().includes("PRIVATE_SECRET"));
    assert.ok(![raw, ...channel.sent].join().includes("password="));
    dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
    assert.equal(posts, 1);
    assert.equal(channel.sent.length, 1);
  });
}

test("a dropped analysis response reconciles a saved report using GET without another analysis or report send", async (t) => {
  const path = await directory(t); const channel = chat(); const calls: string[] = [];
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (url, options) => {
    calls.push(`${options?.method} ${url}`);
    if (options?.method === "POST") throw new Error("private socket error");
    assert.equal(String(url), `http://127.0.0.1:8000/reports/report_photon_${event}`);
    return Response.json({ schema_version: "2.1", job_id: `photon_${event}`, outcome: "matched", submitted: {} });
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  const marker = JSON.parse(await readFile(join(path, `${event}.json`), "utf8"));
  assert.equal(marker.state, "completed");
  assert.equal(marker.report_id, `report_photon_${event}`);
  assert.equal(marker.reconciled, true);
  assert.equal(marker.http_status, null);
  assert.match(channel.sent[0]!, /report reply could not be confirmed.*report remains saved/);
  assert.ok(!channel.sent[0]!.includes("Full report:"));
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  assert.equal(calls.length, 2);
  assert.equal(channel.sent.length, 1);
});

test("a dropped response reconciles a failed Tiger job and retains safe failure fields", async (t) => {
  const path = await directory(t); const channel = chat(); const calls: string[] = [];
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (url, options) => {
    calls.push(`${options?.method} ${url}`);
    if (options?.method === "POST") throw new Error("connection lost");
    if (String(url).includes("/reports/")) return Response.json({}, { status: 404 });
    return Response.json({ job: { id: `photon_${event}`, status: "failed",
      error_code: "TRANSCRIPTION_FAILED", error_stage: "extract_media_features", private: "do-not-echo" } });
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  const raw = await readFile(join(path, `${event}.json`), "utf8");
  const marker = JSON.parse(raw);
  assert.equal(marker.state, "failed");
  assert.equal(marker.error_code, "TRANSCRIPTION_FAILED");
  assert.equal(marker.error_stage, "extract_media_features");
  assert.equal(marker.retryable, null);
  assert.equal(marker.reconciled, true);
  assert.match(channel.sent[0]!, /could not be transcribed/);
  assert.ok(!raw.includes("do-not-echo"));
  assert.equal(calls.filter((call) => call.startsWith("POST ")).length, 1);
  assert.equal(calls.filter((call) => call.startsWith("GET ")).length, 2);
});

for (const kind of ["unknown-error", "invalid-json", "wrong-job", "wrong-report", "null-retryable", "array-delivery-status", "missing-outcome", "wrong-outcome"] as const) {
  test(`${kind} remains uncertain when read-only reconciliation cannot confirm a result`, async (t) => {
    const path = await directory(t); const channel = chat(); let posts = 0; let reads = 0;
    const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (_url, options) => {
      if (options?.method === "GET") { reads++; return Response.json({}, { status: 404 }); }
      posts++;
      if (kind === "invalid-json") return new Response("PRIVATE_NOT_JSON", { status: 500 });
      if (kind === "unknown-error") return Response.json({ error: { code: "PRIVATE_UNKNOWN", stage: "retrieve_post", retryable: false } }, { status: 500 });
      if (kind === "null-retryable") return Response.json({ error: { code: "PROCESSING_FAILED", stage: "retrieve_post", retryable: null } }, { status: 500 });
      return Response.json({ job_id: kind === "wrong-job" ? "other" : `photon_${event}`,
        report_id: kind === "wrong-report" ? "report_other" : `report_photon_${event}`,
        outcome: kind === "missing-outcome" ? undefined : kind === "wrong-outcome" ? "complete" : "matched",
        delivery_status: kind === "array-delivery-status" ? ["accepted"] : "accepted" });
    }) as typeof fetch });
    dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
    const raw = await readFile(join(path, `${event}.json`), "utf8");
    assert.equal(JSON.parse(raw).state, "uncertain");
    assert.match(channel.sent[0]!, /could not confirm completion/);
    assert.ok(![raw, ...channel.sent].join().includes("PRIVATE"));
    dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
    assert.equal(posts, 1);
    assert.equal(reads, 2);
  });
}

test("a still-running Tiger job is described truthfully and never automatically replayed", async (t) => {
  const path = await directory(t); const channel = chat(); let posts = 0;
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (url, options) => {
    if (options?.method === "POST") { posts++; throw new Error("lost response"); }
    if (String(url).includes("/reports/")) return Response.json({}, { status: 404 });
    return Response.json({ job: { id: `photon_${event}`, status: "running", error_code: null, error_stage: null } });
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  const marker = JSON.parse(await readFile(join(path, `${event}.json`), "utf8"));
  assert.equal(marker.state, "uncertain");
  assert.equal(marker.backend_status, "running");
  assert.match(channel.sent[0]!, /still in progress/);
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  assert.equal(posts, 1);
});

test("reconciliation does not coerce a malformed backend job status", async (t) => {
  const path = await directory(t); const channel = chat();
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async (url, options) => {
    if (options?.method === "POST") throw new Error("lost response");
    if (String(url).includes("/reports/")) return Response.json({}, { status: 404 });
    return Response.json({ job: { id: `photon_${event}`, status: ["running"], error_code: null, error_stage: null } });
  }) as typeof fetch });
  dispatcher.enqueue(record, event, channel.space); await dispatcher.idle();
  const marker = JSON.parse(await readFile(join(path, `${event}.json`), "utf8"));
  assert.equal(marker.state, "uncertain");
  assert.equal(marker.backend_status, null);
  assert.match(channel.sent[0]!, /could not confirm completion/);
  assert.ok(!channel.sent[0]!.includes("still in progress"));
});

test("cloud readiness requires the three real backend routes and omits raw failures", async () => {
  const paths = { "/analyze/{job_id}/run": { post: {} }, "/reports/{report_id}": { get: {} }, "/ingest/{job_id}": { post: {}, get: {} } };
  await assertBackendReady(8000, (async (url, options) => {
    assert.equal(String(url), "http://127.0.0.1:8000/openapi.json");
    assert.equal(options?.method, "GET");
    assert.equal(options?.redirect, "error");
    assert.ok(options?.signal instanceof AbortSignal);
    return Response.json({ paths });
  }) as typeof fetch);
  for (const request of [
    async () => { throw new Error("PRIVATE_CONNECTION_SECRET"); },
    async () => Response.json({ paths: {} }),
    async () => Response.json({ paths: { ...paths, "/ingest/{job_id}": { get: {} } } }),
    async () => Response.json({ paths: { ...paths, "/ingest/{job_id}": { post: {} } } }),
    async () => new Response("PRIVATE_BODY", { status: 500 }),
    async () => new Response("PRIVATE_INVALID_JSON"),
  ]) {
    await assert.rejects(assertBackendReady(8000, request as typeof fetch), (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, BACKEND_NOT_READY_MESSAGE);
      return true;
    });
  }
});
