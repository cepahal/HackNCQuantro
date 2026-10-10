import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { Space } from "spectrum-ts";
import { createAnalysisDispatcher } from "../src/analysis.ts";
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
    return Response.json({ job_id: `photon_${event}`, report_id: `report_photon_${event}`, delivery_status: "accepted" });
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
  const dispatcher = createAnalysisDispatcher({ directory: path, backendPort: 8000, request: (async () => {
    calls++; throw new Error("private provider credential details");
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
    job_id: `photon_${event}`, report_id: `report_photon_${event}`, delivery_status: "failed",
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
