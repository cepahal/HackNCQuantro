import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { Space } from "spectrum-ts";
import { createAnalysisDispatcher } from "../src/analysis.ts";
import { RECOVERY_LIMITS, readPrivateJson, savedUrlIntake, withinRecoveryDeadline } from "../src/recovery.ts";
import type { IntakeRecord } from "../src/receive.ts";

const receivedAt = "2026-10-10T17:00:00.000Z";
function intake(messageId = "native-message-1", platform = "imessage") {
  const record: IntakeRecord = {
    schema_version: "photon-intake-1", conversation_id: "native-conversation", message_id: messageId,
    receiving_line: null, input_type: "url", input_url: "https://example.com/post", text: "Check this post",
    attachments: [], status: "received", received_at: receivedAt,
  };
  const event = createHash("sha256").update(JSON.stringify([platform, record.receiving_line, record.conversation_id, record.message_id])).digest("hex");
  return { record, event, jobId: `photon_${event}` };
}
function report(jobId: string) {
  return { schema_version: "2.1", job_id: jobId, outcome: "no_verified_match", submitted: {},
    source: null, alignment: null, context: null, comparison: null, summary: null, limitations: [] };
}
function missing(url: unknown) {
  return String(url).includes("/reports/")
    ? Response.json({ error: { message: "Report not found" } }, { status: 404 })
    : Response.json({ error: { code: "MEDIA_UNAVAILABLE", stage: "retrieve_post", message: "Job not found", retryable: false } }, { status: 404 });
}
async function fixture(t: { after(fn: () => Promise<void>): void }) {
  const root = await mkdtemp(join(tmpdir(), "quantro-recovery-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const directory = join(root, "analysis"), inboxDirectory = join(root, "inbox");
  await mkdir(directory); await mkdir(inboxDirectory);
  const sent: string[] = [];
  const space = { id: "native-conversation", async send(text: string) { sent.push(text); } } as unknown as Space;
  return { root, directory, inboxDirectory, sent, space };
}
async function saveIntake(inbox: string, value = intake()) {
  await mkdir(join(inbox, value.event));
  await writeFile(join(inbox, value.event, "request.json"), JSON.stringify(value.record));
  return value;
}
async function marker(directory: string, event: string, value: unknown) {
  await writeFile(join(directory, `${event}.json`), JSON.stringify(value));
}
async function readMarker(directory: string, event: string) {
  return JSON.parse(await readFile(join(directory, `${event}.json`), "utf8"));
}

for (const state of ["pending", "uncertain", "failed"] as const) {
  test(`startup reconciles ${state} to saved completion without replay or notifications`, async (t) => {
    const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
    await marker(f.directory, value.event, { job_id: value.jobId, state, error_code: "PROCESSING_FAILED", error_stage: "build_report", retryable: true });
    let reads = 0, resolves = 0;
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET"); reads++;
      assert.ok(String(url).endsWith(`/reports/report_${value.jobId}`));
      return Response.json(report(value.jobId));
    }) as typeof fetch });
    const promise = dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { resolves++; return f.space; } });
    assert.equal(dispatcher.recover({ ...f, resumeUnstarted: false }), promise);
    const summary = await promise;
    await dispatcher.idle();
    assert.equal(summary.reconciled, 1); assert.equal(summary.resumed, 0);
    assert.equal(reads, 1); assert.equal(resolves, 0); assert.deepEqual(f.sent, []);
    const saved = await readMarker(f.directory, value.event);
    assert.equal(saved.state, "completed"); assert.equal(saved.report_id, `report_${value.jobId}`);
    assert.equal(saved.error_code, null); assert.equal(saved.error_stage, null); assert.equal(saved.retryable, null);
    assert.equal(saved.reconciled, true);
    dispatcher.enqueue(value.record, value.event, f.space); await dispatcher.idle(); assert.equal(reads, 1);
  });
}

for (const status of ["failed", "running", "ingested", "completed", "unknown"] as const) {
  test(`restart preserves truthful ${status} backend state without a report or sends`, async (t) => {
    const f = await fixture(t), value = intake();
    await marker(f.directory, value.event, { job_id: value.jobId, state: "pending" });
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET");
      if (String(url).includes("/reports/")) return missing(url);
      return Response.json({ job: { id: value.jobId, status, error_code: "SEARCH_FAILED", error_stage: "search_candidates", private: "PRIVATE_SECRET" } });
    }) as typeof fetch });
    const summary = await dispatcher.recover(f);
    const saved = await readMarker(f.directory, value.event);
    assert.equal(saved.state, status === "failed" ? "failed" : "uncertain");
    assert.equal(saved.backend_status, status === "unknown" ? null : status);
    assert.equal(saved.report_id, null); assert.deepEqual(f.sent, []);
    assert.ok(!JSON.stringify(saved).includes("PRIVATE_SECRET"));
    assert.equal(summary.reconciled, status === "failed" ? 1 : 0);
    assert.equal(summary.unresolved, status === "failed" ? 0 : 1);
  });
}

for (const kind of ["rejected-report", "storage-error", "retryable-validation", "wrong-stage", "unknown-error"] as const) {
  test(`completed database job with ${kind} has a truthful recovery classification`, async (t) => {
    const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
    await marker(f.directory, value.event, { job_id: value.jobId, state: "uncertain" });
    let reads = 0;
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET"); reads++;
      if (String(url).includes("/reports/")) return Response.json({ error: {
        code: kind === "storage-error" ? "STORAGE_FAILED" : kind === "unknown-error" ? "PRIVATE_UNKNOWN" : "PROCESSING_FAILED",
        stage: kind === "wrong-stage" ? "build_report" : "display_report",
        retryable: kind === "retryable-validation", message: "PRIVATE_SECRET original report diagnostics",
      } }, { status: 500 });
      return Response.json({ job: { id: value.jobId, status: "completed", error_code: null, error_stage: null } });
    }) as typeof fetch });
    const summary = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { assert.fail("existing marker must not resume"); } });
    const saved = await readMarker(f.directory, value.event);
    const rejected = kind === "rejected-report";
    assert.equal(saved.state, rejected ? "failed" : "uncertain");
    assert.equal(saved.backend_status, "completed");
    assert.equal(saved.error_code, rejected ? "PROCESSING_FAILED" : null);
    assert.equal(saved.error_stage, rejected ? "display_report" : null);
    assert.equal(saved.retryable, rejected ? false : null);
    assert.equal(summary.reconciled, rejected ? 1 : 0); assert.equal(summary.unresolved, rejected ? 0 : 1);
    assert.equal(summary.resumed, 0); assert.equal(reads, 2); assert.deepEqual(f.sent, []);
    assert.ok(!JSON.stringify(saved).includes("PRIVATE"));
    dispatcher.enqueue(value.record, value.event, f.space); await dispatcher.idle(); assert.equal(reads, 2);
  });
}

test("network uncertainty remains uncertain on restart and never triggers another POST", async (t) => {
  const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
  await marker(f.directory, value.event, { job_id: value.jobId, state: "uncertain" });
  let reads = 0;
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (_url, options) => {
    assert.equal(options?.method, "GET"); reads++; throw new Error("PRIVATE_NETWORK_SECRET");
  }) as typeof fetch });
  const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { throw new Error("must not resolve"); } });
  assert.equal(reads, 2); assert.equal(result.unresolved, 1); assert.equal(result.resumed, 0);
  assert.equal((await readMarker(f.directory, value.event)).state, "uncertain");
  assert.deepEqual(f.sent, []);
});

test("saved-but-unstarted native intake resumes once only after both authoritative absence checks", async (t) => {
  const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
  const calls: string[] = [];
  const request = (async (url, options) => {
    calls.push(`${options?.method} ${String(url)}`);
    if (options?.method === "GET") return missing(url);
    const saved = await readMarker(f.directory, value.event);
    assert.equal(saved.state, "pending");
    assert.deepEqual(JSON.parse(String(options?.body)), { url: value.record.input_url, conversation_id: value.record.conversation_id });
    return Response.json({ job_id: value.jobId, report_id: `report_${value.jobId}`, outcome: "no_verified_match", delivery_status: "accepted" });
  }) as typeof fetch;
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request });
  const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async (record) => {
    assert.deepEqual(record, value.record); return f.space;
  } });
  await dispatcher.idle();
  assert.equal(result.resumed, 1); assert.equal(calls.filter((call) => call.startsWith("GET")).length, 2);
  assert.equal(calls.filter((call) => call.startsWith("POST")).length, 1);
  assert.equal((await readMarker(f.directory, value.event)).state, "completed");
  const restarted = createAnalysisDispatcher({ ...f, backendPort: 8000, request });
  await restarted.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => f.space });
  await restarted.idle(); assert.equal(calls.filter((call) => call.startsWith("POST")).length, 1);
  assert.deepEqual(f.sent, []);
});

test("GET-only audit is the default even for provably unstarted intake", async (t) => {
  const f = await fixture(t); await saveIntake(f.inboxDirectory); let reads = 0;
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
    assert.equal(options?.method, "GET"); reads++; return missing(url);
  }) as typeof fetch });
  const result = await dispatcher.recover({ ...f, resolveConversation: async () => { throw new Error("audit must not resolve"); } });
  await dispatcher.idle(); assert.equal(reads, 2); assert.equal(result.resumed, 0); assert.equal(result.unresolved, 1);
  assert.deepEqual(f.sent, []);
});

for (const kind of ["report-exists", "job-running", "job-failed", "unknown-404", "backend-500", "wrong-job", "invalid-json"] as const) {
  test(`marker-free ${kind} cannot be replayed automatically`, async (t) => {
    const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET");
      if (kind === "report-exists" && String(url).includes("/reports/")) return Response.json(report(value.jobId));
      if (kind === "backend-500") return Response.json({ error: "PRIVATE_SECRET" }, { status: 500 });
      if (kind === "unknown-404") return Response.json({}, { status: 404 });
      if (kind === "invalid-json") return new Response("PRIVATE_INVALID_JSON");
      if (String(url).includes("/ingest/") && kind !== "report-exists") return Response.json({ job: {
        id: kind === "wrong-job" ? "other" : value.jobId, status: kind === "job-failed" ? "failed" : "running",
      } });
      return missing(url);
    }) as typeof fetch });
    const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { throw new Error("must not resolve"); } });
    await dispatcher.idle(); assert.equal(result.resumed, 0); assert.deepEqual(f.sent, []);
  });
}

for (const kind of ["completed", "failed", "corrupt", "wrong-id", "oversized", "malformed-state"] as const) {
  test(`an existing ${kind} marker always prevents a fresh POST`, async (t) => {
    const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
    const path = join(f.directory, `${value.event}.json`);
    if (kind === "corrupt") await writeFile(path, "not JSON");
    else if (kind === "oversized") await writeFile(path, "x".repeat(8193));
    else await marker(f.directory, value.event, { job_id: kind === "wrong-id" ? "other" : value.jobId,
      state: kind === "malformed-state" ? ["pending"] : kind, error_code: "MEDIA_UNAVAILABLE", error_stage: "retrieve_post", retryable: false });
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET"); return missing(url);
    }) as typeof fetch });
    const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { throw new Error("must not resolve"); } });
    await dispatcher.idle(); assert.equal(result.resumed, 0); assert.deepEqual(f.sent, []);
  });
}

for (const kind of ["terminal", "wrong-hash", "attachment", "incomplete", "oversized", "private-credentials"] as const) {
  test(`recovery excludes ${kind} intake without any backend lookup`, async (t) => {
    const f = await fixture(t), value = intake("native-message-1", kind === "terminal" ? "terminal" : "imessage");
    if (kind === "wrong-hash") value.event = "f".repeat(64);
    if (kind === "attachment") value.record.input_type = "attachment";
    if (kind === "private-credentials") value.record.input_url = "https://user:password@example.com/post";
    await mkdir(join(f.inboxDirectory, value.event));
    if (kind !== "incomplete") await writeFile(join(f.inboxDirectory, value.event, "request.json"),
      kind === "oversized" ? "x".repeat(65537) : JSON.stringify(value.record));
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async () => { assert.fail("invalid intake must not make requests"); }) as typeof fetch });
    const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => f.space });
    assert.equal(result.resumed, 0); assert.deepEqual(f.sent, []);
  });
}

for (const kind of ["wrong-conversation", "resolver-failure"] as const) {
  test(`${kind} prevents automatic resume after absence checks`, async (t) => {
    const f = await fixture(t); await saveIntake(f.inboxDirectory);
    const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
      assert.equal(options?.method, "GET"); return missing(url);
    }) as typeof fetch });
    const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => {
      if (kind === "resolver-failure") throw new Error("PRIVATE_SDK_SECRET");
      return { ...f.space, id: "other" };
    } });
    assert.equal(result.resumed, 0); assert.deepEqual(f.sent, []);
  });
}

test("active ordinary dispatch is never overwritten or re-enqueued by startup recovery", async (t) => {
  const f = await fixture(t), value = await saveIntake(f.inboxDirectory); let posts = 0;
  let release!: () => void; const hold = new Promise<void>((resolve) => { release = resolve; });
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (_url, options) => {
    assert.equal(options?.method, "POST"); posts++; await hold;
    return Response.json({ job_id: value.jobId, report_id: `report_${value.jobId}`, outcome: "no_verified_match", delivery_status: "accepted" });
  }) as typeof fetch });
  dispatcher.enqueue(value.record, value.event, f.space);
  await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => f.space });
  release(); await dispatcher.idle(); assert.equal(posts, 1);
  assert.equal((await readMarker(f.directory, value.event)).state, "completed");
});

test("recovery caps status lookups at 32 jobs without loading an entire directory", async (t) => {
  const f = await fixture(t); let reads = 0;
  for (let i = 0; i < 40; i++) {
    const value = intake(`message-${i}`); await marker(f.directory, value.event, { job_id: value.jobId, state: "pending" });
  }
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
    assert.equal(options?.method, "GET"); reads++; return missing(url);
  }) as typeof fetch });
  const result = await dispatcher.recover(f);
  assert.equal(result.limited, true); assert.equal(reads, 2 * RECOVERY_LIMITS.jobs);
  assert.equal(result.scanned, RECOVERY_LIMITS.jobs); assert.deepEqual(f.sent, []);
});

test("recovery caps scanned directory entries at 256 even for unrelated files", async (t) => {
  const f = await fixture(t);
  for (let i = 0; i < 270; i++) await writeFile(join(f.directory, `unrelated-${i}`), "ignored");
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async () => { assert.fail("no lookup expected"); }) as typeof fetch });
  const result = await dispatcher.recover(f);
  assert.equal(result.limited, true); assert.equal(result.scanned, RECOVERY_LIMITS.entries);
});

test("symlinked markers and request records are not followed", async (t) => {
  const f = await fixture(t), value = intake(); const target = join(f.root, "target.json");
  await writeFile(target, JSON.stringify(value.record));
  try { await symlink(target, join(f.directory, `${value.event}.json`), "file"); }
  catch (error) {
    if (error instanceof Error && "code" in error && ["EPERM", "EACCES"].includes(String(error.code))) { t.skip("Creating file symlinks requires Windows developer-mode permission"); return; }
    throw error;
  }
  await assert.rejects(readPrivateJson(join(f.directory, `${value.event}.json`), 65536));
  await mkdir(join(f.inboxDirectory, value.event));
  await symlink(target, join(f.inboxDirectory, value.event, "request.json"), "file");
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async () => { assert.fail("symlinks must not trigger requests"); }) as typeof fetch });
  assert.equal((await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => f.space })).resumed, 0);
  assert.equal(await readFile(target, "utf8"), JSON.stringify(value.record));
});

test("a symlinked recovery directory root is not scanned", async (t) => {
  const f = await fixture(t), value = intake();
  await marker(f.directory, value.event, { job_id: value.jobId, state: "pending" });
  const linked = join(f.root, "linked-analysis");
  try { await symlink(f.directory, linked, process.platform === "win32" ? "junction" : "dir"); }
  catch (error) {
    if (error instanceof Error && "code" in error && ["EPERM", "EACCES"].includes(String(error.code))) { t.skip("Creating directory links is unavailable"); return; }
    throw error;
  }
  const dispatcher = createAnalysisDispatcher({ directory: linked, backendPort: 8000,
    request: (async () => { assert.fail("linked root must not trigger a status lookup"); }) as typeof fetch });
  assert.equal((await dispatcher.recover(f)).scanned, 0);
  assert.equal((await readMarker(f.directory, value.event)).state, "pending");
});

test("oversized status bodies cannot authorize a resumed POST or leak their content", async (t) => {
  const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
  await marker(f.directory, value.event, { job_id: value.jobId, state: "uncertain" });
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (_url, options) => {
    assert.equal(options?.method, "GET");
    return new Response(JSON.stringify({ private: "PRIVATE_SECRET", padding: "x".repeat(2 * 1024 * 1024) }));
  }) as typeof fetch });
  const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => { assert.fail("oversized state cannot authorize resume"); } });
  assert.equal(result.resumed, 0); assert.equal(result.unresolved, 1);
  const saved = await readMarker(f.directory, value.event);
  assert.equal(saved.state, "uncertain"); assert.ok(!JSON.stringify(saved).includes("PRIVATE_SECRET"));
  assert.deepEqual(f.sent, []);
});

test("a stale temporary marker cannot prevent a later authoritative reconciliation", async (t) => {
  const f = await fixture(t), value = intake();
  await marker(f.directory, value.event, { job_id: value.jobId, state: "pending" });
  const stalePath = join(f.directory, `${value.event}.json.tmp`);
  await writeFile(stalePath, "old interrupted write");
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (_url, options) => {
    assert.equal(options?.method, "GET"); return Response.json(report(value.jobId));
  }) as typeof fetch });
  assert.equal((await dispatcher.recover(f)).reconciled, 1);
  assert.equal((await readMarker(f.directory, value.event)).state, "completed");
  assert.equal(await readFile(stalePath, "utf8"), "old interrupted write");
});

test("a concurrent durable claim after conversation resolution prevents recovery from enqueueing", async (t) => {
  const f = await fixture(t), value = await saveIntake(f.inboxDirectory);
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
    assert.equal(options?.method, "GET"); return missing(url);
  }) as typeof fetch });
  const result = await dispatcher.recover({ ...f, resumeUnstarted: true, resolveConversation: async () => {
    await marker(f.directory, value.event, { job_id: value.jobId, state: "pending" }); return f.space;
  } });
  assert.equal(result.resumed, 0); await dispatcher.idle(); assert.deepEqual(f.sent, []);
});

test("recovery deadlines bound a stalled operation and reject an exhausted budget immediately", async () => {
  const start = Date.now();
  await assert.rejects(withinRecoveryDeadline(() => new Promise(() => undefined), Date.now() + 25));
  assert.ok(Date.now() - start < 1000);
  let called = false;
  await assert.rejects(withinRecoveryDeadline(async () => { called = true; }, Date.now() - 1));
  assert.equal(called, false);
  assert.equal(RECOVERY_LIMITS.milliseconds, 60_000);
});

test("native intake parser rejects missing null field and non-UTC timestamps", () => {
  const value = intake(); assert.deepEqual(savedUrlIntake(value.record, value.event), value.record);
  const { receiving_line: _line, ...missingLine } = value.record;
  assert.equal(savedUrlIntake(missingLine, value.event), undefined);
  assert.equal(savedUrlIntake({ ...value.record, received_at: "2026-10-10T17:00:00-07:00" }, value.event), undefined);
});

test("a recognized HTTP500 after report commit remains completed and does not resend the summary", async (t) => {
  const f = await fixture(t), value = intake(); let posts = 0;
  const dispatcher = createAnalysisDispatcher({ ...f, backendPort: 8000, request: (async (url, options) => {
    if (options?.method === "POST") {
      posts++; return Response.json({ error: { code: "STORAGE_FAILED", stage: "save_report_and_complete_job", retryable: true,
        message: "PRIVATE_SECRET database credentials" } }, { status: 500 });
    }
    assert.ok(String(url).endsWith(`/reports/report_${value.jobId}`)); return Response.json(report(value.jobId));
  }) as typeof fetch });
  dispatcher.enqueue(value.record, value.event, f.space); await dispatcher.idle();
  const saved = await readMarker(f.directory, value.event);
  assert.equal(saved.state, "completed"); assert.equal(saved.http_status, 500);
  assert.equal(saved.error_code, null); assert.equal(saved.error_stage, null); assert.equal(saved.retryable, null);
  assert.match(f.sent[0]!, /analysis is complete.*report remains saved/);
  assert.ok(!f.sent.join().includes("PRIVATE_SECRET")); assert.ok(!f.sent.join().includes("Full report:"));
  dispatcher.enqueue(value.record, value.event, f.space); await dispatcher.idle(); assert.equal(posts, 1); assert.equal(f.sent.length, 1);
});
