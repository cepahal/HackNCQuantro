import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { createEnrollmentHandler, EnrollmentError, WELCOME_MESSAGE, type EnrollmentDiagnostic, type WelcomeDiagnostic } from "../src/enroll.ts";
import { createBridgeServer } from "../src/server.ts";

const projectId = "12345678-1234-4123-8123-123456789abc";
const projectSecret = "test-only-project-secret";
const phone = "+15551234567";
const assigned = "+15557654321";
const userId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const key = `enroll_${createHash("sha256").update(phone).digest("hex")}`;
const legacyLedgerName = `${createHash("sha256").update(`${projectId}:${phone}`).digest("hex")}.json`;
const ledgerName = (id = userId): string => `${createHash("sha256").update(`${projectId}:${phone}:${id}`).digest("hex")}.json`;
const input = { phone_number: phone, consent: true };
const user = { type: "shared", phoneNumber: phone, assignedPhoneNumber: assigned, id: userId };
const welcomeRequestId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const explicitKey = (id = welcomeRequestId): string => `enroll_${createHash("sha256").update(`${phone}:${id.toLowerCase()}`).digest("hex")}`;
const explicitLedgerName = (id = welcomeRequestId): string => `${createHash("sha256").update(`${projectId}:${phone}:${id.toLowerCase()}`).digest("hex")}.json`;
const explicitInput = { ...input, welcome_request_id: welcomeRequestId };

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

function hasError(status: number, code: string) {
  return (error: unknown): boolean => {
    assert.ok(error instanceof EnrollmentError);
    assert.equal(error.status, status);
    assert.equal(error.body.error.code, code);
    assert.equal(error.body.error.stage, "enroll");
    assert.doesNotMatch(JSON.stringify(error.body), /test-only-project-secret|private-user-id|provider-private-detail/);
    return true;
  };
}

async function fixture(t: TestContext, explicitRequestId?: string) {
  const directory = await mkdtemp(join(tmpdir(), "quantro-enroll-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const calls: { url: string; options: RequestInit }[] = [];
  const sends: { recipient: string; text: string }[] = [];
  const diagnostics: WelcomeDiagnostic[] = [];
  const enrollmentDiagnostics: EnrollmentDiagnostic[] = [];
  let listed: unknown = { succeed: true, data: { users: [], total: 0 } };
  let created: unknown = { succeed: true, data: user };
  const options = {
    projectId, projectSecret, deliveryDirectory: directory,
    diagnostic: (event: WelcomeDiagnostic) => { diagnostics.push(event); },
    enrollmentDiagnostic: (event: EnrollmentDiagnostic) => { enrollmentDiagnostics.push(event); },
    fetch: (async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), options: init! });
      if (init?.method === "GET") return json(listed);
      const createdUser = (created as { data?: unknown })?.data;
      if (createdUser) listed = { succeed: true, data: { users: [createdUser], total: 1 } };
      return json(created);
    }) as typeof fetch,
    sendWelcome: async (recipient: string, text: string) => {
      const persisted = JSON.parse(await readFile(join(directory, explicitRequestId ? explicitLedgerName(explicitRequestId) : ledgerName()), "utf8"));
      assert.equal(persisted.state, "pending");
      sends.push({ recipient, text });
      return { id: "welcome-message-id" };
    },
  };
  return { directory, calls, sends, diagnostics, enrollmentDiagnostics, options,
    setListed: (value: unknown) => { listed = value; }, setCreated: (value: unknown) => { created = value; } };
}

test("enrolls through fixed official API before one welcome and persists safe reply", async (t) => {
  const state = await fixture(t);
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.deepEqual(result, { assigned_phone_number: assigned, open_messages_url: `sms:${assigned}&body=Hi%20Quantro`,
    welcome_status: "accepted", already_enrolled: false });
  assert.equal(state.calls.length, 2);
  const [lookup, create] = state.calls;
  assert.equal(lookup!.options.method, "GET");
  const lookupUrl = new URL(lookup!.url);
  assert.equal(lookupUrl.hostname, "spectrum.photon.codes");
  assert.equal(lookupUrl.searchParams.get("search"), phone);
  assert.equal(lookupUrl.searchParams.get("type"), "shared");
  assert.equal(create!.url, `https://spectrum.photon.codes/projects/${projectId}/users/`);
  assert.deepEqual(JSON.parse(String(create!.options.body)), { type: "shared", phoneNumber: phone });
  assert.equal((create!.options.headers as Record<string, string>).authorization,
    `Basic ${Buffer.from(`${projectId}:${projectSecret}`).toString("base64")}`);
  assert.equal(create!.options.redirect, "error");
  assert.ok(create!.options.signal instanceof AbortSignal);
  assert.deepEqual(state.sends, [{ recipient: phone, text: WELCOME_MESSAGE }]);
  assert.doesNotMatch(WELCOME_MESSAGE, /STOP|opt.out/i);
  const persisted = await readFile(join(state.directory, ledgerName()), "utf8");
  assert.equal(JSON.parse(persisted).state, "accepted");
  assert.doesNotMatch(persisted, /15551234567|test-only-project-secret|aaaaaaaa-aaaa/);
  assert.doesNotMatch(JSON.stringify(result), /aaaaaaaa-aaaa|welcome-message-id|test-only-project-secret/);
});

test("existing provider user without receipt history is recognized without create or welcome", async (t) => {
  const state = await fixture(t);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(result.already_enrolled, true);
  assert.equal(result.welcome_status, "uncertain");
  assert.equal(state.calls.length, 1);
  assert.equal(state.sends.length, 0);
  assert.equal(JSON.parse(await readFile(join(state.directory, ledgerName()), "utf8")).state, "uncertain");
  assert.equal((await createEnrollmentHandler(state.options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.calls.length, 2);
});

test("explicit welcome intent sends to an existing user while preserving previous uncertain receipts", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const old = JSON.stringify({ version: 1, state: "uncertain", assigned_phone_number: assigned });
  await writeFile(join(state.directory, ledgerName()), old);
  await writeFile(join(state.directory, legacyLedgerName), old);
  const result = await createEnrollmentHandler(state.options)(explicitInput, explicitKey());
  assert.equal(result.already_enrolled, true);
  assert.equal(result.welcome_status, "accepted");
  assert.equal(state.calls.length, 1);
  assert.deepEqual(state.sends, [{ recipient: phone, text: WELCOME_MESSAGE }]);
  assert.equal(await readFile(join(state.directory, ledgerName()), "utf8"), old);
  assert.equal(await readFile(join(state.directory, legacyLedgerName), "utf8"), old);
  const receipt = JSON.parse(await readFile(join(state.directory, explicitLedgerName()), "utf8"));
  assert.equal(receipt.state, "accepted");
  assert.equal(receipt.message_id, "welcome-message-id");
});

test("one explicit request coalesces concurrent signup and remains deduplicated after restart", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const enroll = createEnrollmentHandler(state.options);
  const results = await Promise.all(Array.from({ length: 10 }, () => enroll(explicitInput, explicitKey())));
  assert.ok(results.every(result => result.welcome_status === "accepted" && result.already_enrolled));
  assert.equal(state.calls.length, 1);
  assert.equal(state.sends.length, 1);
  assert.equal((await createEnrollmentHandler(state.options)(explicitInput, explicitKey())).welcome_status, "accepted");
  assert.equal(state.calls.length, 2);
  assert.equal(state.sends.length, 1);
});

test("uppercase UUID spelling is normalized to the same welcome request and idempotency key", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const enroll = createEnrollmentHandler(state.options);
  const results = await Promise.all([
    enroll(explicitInput, explicitKey()),
    enroll({ ...explicitInput, welcome_request_id: welcomeRequestId.toUpperCase() }, explicitKey()),
  ]);
  assert.ok(results.every(result => result.welcome_status === "accepted"));
  assert.equal(state.calls.length, 1);
  assert.equal(state.sends.length, 1);
  assert.equal((await createEnrollmentHandler(state.options)({
    ...explicitInput, welcome_request_id: welcomeRequestId.toUpperCase(),
  }, explicitKey())).welcome_status, "accepted");
  assert.equal(state.sends.length, 1);
});

test("the same explicit request never sends again after provider user revocation and recreation", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  assert.equal((await createEnrollmentHandler(state.options)(explicitInput, explicitKey())).welcome_status, "accepted");
  const receipt = await readFile(join(state.directory, explicitLedgerName()), "utf8");
  state.setListed({ succeed: true, data: { users: [], total: 0 } });
  state.setCreated({ succeed: true, data: { ...user, id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" } });
  const restored = await createEnrollmentHandler(state.options)(explicitInput, explicitKey());
  assert.equal(restored.already_enrolled, false);
  assert.equal(restored.welcome_status, "uncertain");
  assert.equal(state.sends.length, 1);
  assert.equal(await readFile(join(state.directory, explicitLedgerName()), "utf8"), receipt);
  assert.equal((await createEnrollmentHandler(state.options)(explicitInput, explicitKey())).welcome_status, "uncertain");
  assert.equal(state.sends.length, 1);
});

test("independent handlers share the durable explicit pending claim before a send", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  let release!: (value: { id: string }) => void;
  let started!: () => void;
  const sending = new Promise<void>(resolve => { started = resolve; });
  let sends = 0;
  const options = { ...state.options, sendWelcome: async () => {
    sends++;
    started();
    return new Promise<{ id: string }>(resolve => { release = resolve; });
  } };
  const first = createEnrollmentHandler(options)(explicitInput, explicitKey());
  await sending;
  const second = await createEnrollmentHandler(options)(explicitInput, explicitKey());
  assert.equal(second.welcome_status, "uncertain");
  assert.equal(sends, 1);
  release({ id: "explicit-accepted" });
  assert.equal((await first).welcome_status, "accepted");
});

for (const kind of ["pending", "timeout", "exception"] as const) {
  test(`same explicit request with ${kind} delivery never sends another welcome`, async (t) => {
    const state = await fixture(t, welcomeRequestId);
    state.setListed({ succeed: true, data: { users: [user], total: 1 } });
    let sends = 0;
    if (kind === "pending") await writeFile(join(state.directory, explicitLedgerName()), JSON.stringify({
      version: 1, state: "pending", assigned_phone_number: assigned,
    }));
    const options = { ...state.options, sendTimeoutMs: 5, sendWelcome: async () => {
      sends++;
      if (kind === "timeout") return new Promise<never>(() => {});
      throw new Error("PRIVATE_PROVIDER_DETAILS");
    } };
    assert.equal((await createEnrollmentHandler(options)(explicitInput, explicitKey())).welcome_status, "uncertain");
    assert.equal((await createEnrollmentHandler(options)(explicitInput, explicitKey())).welcome_status, "uncertain");
    assert.equal(sends, kind === "pending" ? 0 : 1);
  });
}

test("a distinct explicit welcome request has a separate receipt without rewriting the first", async (t) => {
  const state = await fixture(t, welcomeRequestId);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const old = JSON.stringify({ version: 1, state: "accepted", assigned_phone_number: assigned, message_id: "older-welcome" });
  await writeFile(join(state.directory, explicitLedgerName()), old);
  const next = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  let sends = 0;
  const options = { ...state.options, sendWelcome: async () => {
    assert.equal(JSON.parse(await readFile(join(state.directory, explicitLedgerName(next)), "utf8")).state, "pending");
    sends++;
    return { id: "new-welcome" };
  } };
  assert.equal((await createEnrollmentHandler(options)({ ...input, welcome_request_id: next }, explicitKey(next))).welcome_status, "accepted");
  assert.equal(sends, 1);
  assert.equal(await readFile(join(state.directory, explicitLedgerName()), "utf8"), old);
});

for (const id of [null, true, "", "not-a-uuid", "bbbbbbbb-bbbb-1bbb-8bbb-bbbbbbbbbbbb", "bbbbbbbb-bbbb-4bbb-7bbb-bbbbbbbbbbbb", `${welcomeRequestId}\n`]) {
  test(`invalid explicit welcome request ${JSON.stringify(id)} is rejected before provider interaction`, async (t) => {
    const state = await fixture(t);
    await assert.rejects(createEnrollmentHandler(state.options)({ ...input, welcome_request_id: id }, key), hasError(400, "INVALID_ENROLLMENT_INPUT"));
    assert.equal(state.calls.length, 0);
    assert.equal(state.sends.length, 0);
  });
}

test("explicit intent requires its matching idempotency key and rejects unknown fields", async (t) => {
  const state = await fixture(t);
  await assert.rejects(createEnrollmentHandler(state.options)(explicitInput, key), hasError(400, "INVALID_IDEMPOTENCY_KEY"));
  await assert.rejects(createEnrollmentHandler(state.options)({ ...explicitInput, retry: true }, explicitKey()), hasError(400, "INVALID_ENROLLMENT_INPUT"));
  assert.equal(state.calls.length, 0);
  assert.equal(state.sends.length, 0);
});

test("enrollment diagnostics separate lookup and create without retaining private data", async (t) => {
  const state = await fixture(t);
  assert.equal((await createEnrollmentHandler(state.options)(input, key)).welcome_status, "accepted");
  assert.deepEqual(state.enrollmentDiagnostics.map(({ phase, category, http_status }) => ({ phase, category, http_status })), [
    { phase: "lookup", category: "response_ok", http_status: 200 },
    { phase: "create", category: "response_ok", http_status: 200 },
  ]);
  for (const event of state.enrollmentDiagnostics) {
    assert.deepEqual(Object.keys(event).sort(), ["category", "elapsed_ms", "http_status", "phase"]);
    assert.ok(Number.isInteger(event.elapsed_ms) && event.elapsed_ms >= 0);
  }
  assert.doesNotMatch(JSON.stringify(state.enrollmentDiagnostics), /15551234567|15557654321|test-only|aaaaaaaa|12345678|https|Basic/);
});

for (const phase of ["lookup", "create"] as const) {
  for (const category of ["http_error", "invalid_response", "timeout", "network_error"] as const) {
    test(`${phase} ${category} is diagnosed once without provider details or retries`, async (t) => {
      const state = await fixture(t);
      let requests = 0;
      const options = { ...state.options, requestTimeoutMs: 10, fetch: (async (url, init) => {
        requests++;
        if (phase === "create" && init?.method === "GET") return json({ succeed: true, data: { users: [] } });
        if (category === "http_error") return new Response(`PRIVATE_PROVIDER_DETAILS ${phone}`, { status: 403 });
        if (category === "invalid_response") return json({ succeed: false, details: { phone, projectSecret, userId } });
        if (category === "timeout") return new Promise<never>((_resolve, reject) => {
          const signal = init!.signal!;
          signal.addEventListener("abort", () => reject(new Error(`PRIVATE_PROVIDER_DETAILS ${phone}`)), { once: true });
        });
        throw Object.assign(new Error(`PRIVATE_PROVIDER_DETAILS ${phone} ${projectSecret}`), { url, projectId, userId });
      }) as typeof fetch };
      // AbortSignal.timeout is unrefed; keep the process alive while the fake fetch waits.
      const keepAlive = setTimeout(() => {}, 1_000);
      try {
        await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
      } finally { clearTimeout(keepAlive); }
      assert.equal(requests, phase === "lookup" ? 1 : 2);
      assert.equal(state.sends.length, 0);
      const event = state.enrollmentDiagnostics.at(-1)!;
      assert.equal(event.phase, phase);
      assert.equal(event.category, category);
      assert.equal(event.http_status, category === "http_error" ? 403 : category === "invalid_response" ? 200 : undefined);
      assert.ok(Number.isInteger(event.elapsed_ms) && event.elapsed_ms >= 0);
      assert.doesNotMatch(JSON.stringify(state.enrollmentDiagnostics), /PRIVATE_PROVIDER|15551234567|test-only|aaaaaaaa|12345678|https|Basic/);
    });
  }
}

test("default enrollment logging exposes only fixed phase and response fields", async (t) => {
  const state = await fixture(t);
  const logged: unknown[][] = [];
  t.mock.method(console, "info", (...values: unknown[]) => { logged.push(values); });
  const options = { ...state.options, enrollmentDiagnostic: undefined };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "accepted");
  assert.equal(logged.length, 2);
  assert.ok(logged.every(([label]) => label === "Photon enrollment diagnostic:"));
  assert.deepEqual(logged.map(([, value]) => JSON.parse(String(value)).phase), ["lookup", "create"]);
  assert.doesNotMatch(JSON.stringify(logged), /15551234567|15557654321|test-only|aaaaaaaa|12345678|https|Basic/);
});

for (const asynchronous of [false, true]) {
  test(`${asynchronous ? "async" : "sync"} enrollment diagnostic failure cannot break signup or resend`, async (t) => {
    const state = await fixture(t);
    const options = { ...state.options, enrollmentDiagnostic: () => {
      if (asynchronous) return Promise.reject(new Error("PRIVATE_DIAGNOSTIC_FAILURE"));
      throw new Error("PRIVATE_DIAGNOSTIC_FAILURE");
    } };
    assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "accepted");
    assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "accepted");
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(state.sends.length, 1);
  });
}

test("duplicate after restart reconciles active provider user and sends no further message", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(result.welcome_status, "accepted");
  assert.equal(result.already_enrolled, true);
  assert.equal(state.calls.length, 3);
  assert.equal(state.sends.length, 1);
});

test("revoked cached enrollment is recreated with one fresh welcome for its new provider ID", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  const oldReceipt = await readFile(join(state.directory, ledgerName()), "utf8");
  const newUserId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const newAssigned = "+15559876543";
  state.setListed({ succeed: true, data: { users: [], total: 0 } });
  state.setCreated({ succeed: true, data: { ...user, id: newUserId, assignedPhoneNumber: newAssigned } });
  let sends = 0;
  const options = { ...state.options, sendWelcome: async () => {
    assert.equal(JSON.parse(await readFile(join(state.directory, ledgerName(newUserId)), "utf8")).state, "pending");
    sends++;
    return { id: "new-enrollment-welcome" };
  } };
  const enroll = createEnrollmentHandler(options);
  const results = await Promise.all(Array.from({ length: 10 }, () => enroll(input, key)));
  assert.ok(results.every((result) => result.assigned_phone_number === newAssigned && result.welcome_status === "accepted"));
  assert.equal(results[0]!.already_enrolled, false);
  assert.equal(sends, 1);
  assert.equal(state.calls.filter((call) => call.options.method === "POST").length, 2);
  const retry = await createEnrollmentHandler(options)(input, key);
  assert.equal(retry.already_enrolled, true);
  assert.equal(retry.welcome_status, "accepted");
  assert.equal(retry.assigned_phone_number, newAssigned);
  assert.equal(sends, 1);
  assert.equal(await readFile(join(state.directory, ledgerName()), "utf8"), oldReceipt);
  assert.equal((await readdir(state.directory)).length, 2);
});

for (const receiptState of ["accepted", "uncertain"] as const) {
  test(`restored same-ID enrollment with ${receiptState} history uses current registration without resend`, async (t) => {
    const state = await fixture(t);
    const receipt = JSON.stringify({ version: 1, state: receiptState, assigned_phone_number: assigned,
      ...(receiptState === "accepted" ? { message_id: "historical-welcome" } : {}) });
    await writeFile(join(state.directory, ledgerName()), receipt);
    const newAssigned = "+15559876543";
    state.setCreated({ succeed: true, data: { ...user, assignedPhoneNumber: newAssigned } });
    const enroll = createEnrollmentHandler(state.options);
    const results = await Promise.all(Array.from({ length: 8 }, () => enroll(input, key)));
    assert.ok(results.every((result) => result.already_enrolled === false
      && result.welcome_status === "uncertain" && result.assigned_phone_number === newAssigned));
    assert.equal(results[0]!.open_messages_url, `sms:${newAssigned}&body=Hi%20Quantro`);
    assert.deepEqual(state.calls.map((call) => call.options.method), ["GET", "POST"]);
    assert.equal(state.sends.length, 0);
    assert.deepEqual(state.diagnostics, []);
    assert.equal(await readFile(join(state.directory, ledgerName()), "utf8"), receipt);
    const retry = await createEnrollmentHandler(state.options)(input, key);
    assert.equal(retry.already_enrolled, true);
    assert.equal(retry.welcome_status, "uncertain");
    assert.equal(state.sends.length, 0);
    assert.equal(await readFile(join(state.directory, ledgerName()), "utf8"), receipt);
  });
}

test("cached success fails closed if current provider lookup is unavailable", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  let requests = 0;
  const options = { ...state.options, fetch: (async () => {
    requests++;
    return json({ message: "provider-private-detail" }, 503);
  }) as typeof fetch };
  await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
  assert.equal(requests, 1);
  assert.equal(state.sends.length, 1);
});

test("current assigned number overrides a stale accepted receipt without another welcome", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  const newAssigned = "+15559876543";
  state.setListed({ succeed: true, data: { users: [{ ...user, assignedPhoneNumber: newAssigned }], total: 1 } });
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(result.assigned_phone_number, newAssigned);
  assert.equal(result.open_messages_url, `sms:${newAssigned}&body=Hi%20Quantro`);
  assert.equal(result.welcome_status, "uncertain");
  assert.equal(state.sends.length, 1);
  const retry = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(retry.welcome_status, "uncertain");
  // Returning to the original line must not resurrect the historical receipt.
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  assert.equal((await createEnrollmentHandler(state.options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.sends.length, 1);
});

test("same-ID restoration on the same line remains uncertain across retries and restarts", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  const receipt = await readFile(join(state.directory, ledgerName()), "utf8");
  state.setListed({ succeed: true, data: { users: [], total: 0 } });
  const restored = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(restored.already_enrolled, false);
  assert.equal(restored.welcome_status, "uncertain");
  for (let index = 0; index < 3; index++) {
    const retry = await createEnrollmentHandler(state.options)(input, key);
    assert.equal(retry.already_enrolled, true);
    assert.equal(retry.welcome_status, "uncertain");
  }
  assert.equal(state.sends.length, 1);
  assert.equal(await readFile(join(state.directory, ledgerName()), "utf8"), receipt);
  const marker = JSON.parse(await readFile(join(state.directory, `${ledgerName()}.reconciled.json`), "utf8"));
  assert.deepEqual(marker, { version: 1, state: "uncertain", assigned_phone_number: assigned });
});

test("corrupt reconciliation state fails closed without another welcome", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  await writeFile(join(state.directory, `${ledgerName()}.reconciled.json`), "invalid-private-state");
  await assert.rejects(createEnrollmentHandler(state.options)(input, key), hasError(503, "ENROLLMENT_STORAGE_UNAVAILABLE"));
  assert.equal(state.sends.length, 1);
});

test("legacy receipt is preserved while an active user without a generation receipt stays uncertain", async (t) => {
  const state = await fixture(t);
  const legacy = JSON.stringify({ version: 1, state: "accepted", assigned_phone_number: assigned, message_id: "legacy-welcome" });
  await writeFile(join(state.directory, legacyLedgerName), legacy);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(result.already_enrolled, true);
  assert.equal(result.welcome_status, "uncertain");
  assert.equal(state.sends.length, 0);
  assert.equal(await readFile(join(state.directory, legacyLedgerName), "utf8"), legacy);
  assert.equal(JSON.parse(await readFile(join(state.directory, ledgerName()), "utf8")).state, "uncertain");
});

test("revoked cached enrollment cannot return success when recreation fails", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  const methods: string[] = [];
  const options = { ...state.options, fetch: (async (_url, init) => {
    methods.push(init!.method!);
    return init?.method === "GET" ? json({ succeed: true, data: { users: [], total: 0 } })
      : json({ message: "provider-private-detail" }, 409);
  }) as typeof fetch };
  await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(409, "ENROLLMENT_CAPACITY_OR_CONFLICT"));
  assert.deepEqual(methods, ["GET", "POST"]);
  assert.equal(state.sends.length, 1);
});

test("concurrent calls on one instance share one registration and welcome", async (t) => {
  const state = await fixture(t);
  const enroll = createEnrollmentHandler(state.options);
  const results = await Promise.all(Array.from({ length: 12 }, () => enroll(input, key)));
  assert.equal(state.calls.length, 2);
  assert.equal(state.sends.length, 1);
  assert.ok(results.every((result) => result.welcome_status === "accepted"));
});

test("independent instance sees pending marker and never duplicates a send", async (t) => {
  const state = await fixture(t);
  let entered!: () => void;
  let release!: () => void;
  const started = new Promise<void>((resolve) => { entered = resolve; });
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let sends = 0;
  const options = { ...state.options, sendWelcome: async () => { sends++; entered(); await gate; return { id: "once" }; } };
  const first = createEnrollmentHandler(options)(input, key);
  await started;
  try {
    const second = await createEnrollmentHandler(options)(input, key);
    assert.equal(second.welcome_status, "uncertain");
    assert.equal(second.already_enrolled, true);
  } finally {
    release();
  }
  assert.equal((await first).welcome_status, "accepted");
  assert.equal(sends, 1);
});

for (const mode of ["throw", "missing-id", "invalid-id", "timeout"] as const) {
  test(`${mode} welcome remains uncertain and is never automatically retried`, async (t) => {
    const state = await fixture(t);
    let sends = 0;
    const options = { ...state.options, sendTimeoutMs: 5, sendWelcome: async () => {
      sends++;
      if (mode === "throw") throw new Error("provider-private-detail");
      if (mode === "timeout") return new Promise<never>(() => {});
      return mode === "invalid-id" ? { id: "\n" } : undefined;
    } };
    const first = await createEnrollmentHandler(options)(input, key);
    const second = await createEnrollmentHandler(options)(input, key);
    assert.equal(first.welcome_status, "uncertain");
    assert.equal(second.welcome_status, "uncertain");
    assert.equal(second.already_enrolled, true);
    assert.equal(sends, 1);
    assert.equal(state.diagnostics.length, 1);
    assert.equal(state.diagnostics[0]!.category, mode === "throw" ? "sdk_exception"
      : mode === "timeout" ? "send_deadline" : "invalid_message_id");
    assert.ok(state.diagnostics[0]!.elapsed_ms >= (mode === "timeout" ? 4 : 0));
    assert.equal(JSON.parse(await readFile(join(state.directory, ledgerName()), "utf8")).state, "uncertain");
  });
}

for (const code of [0, 7, 16, -1, 17, 1.5, "7", NaN, Infinity]) {
  test(`welcome diagnostics allow only numeric gRPC status ${String(code)} without private data`, async (t) => {
    const state = await fixture(t);
    const options = { ...state.options, sendWelcome: async () => {
      throw Object.assign(new Error(`provider-private-detail ${phone} ${projectSecret}`), {
        code, details: "PRIVATE_PROVIDER_DETAILS", context: { phone, userId }, projectId,
      });
    } };
    assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
    assert.equal(state.diagnostics.length, 1);
    const event = state.diagnostics[0]!;
    assert.equal(event.category, "sdk_exception");
    assert.ok(Number.isInteger(event.elapsed_ms) && event.elapsed_ms >= 0);
    const valid = typeof code === "number" && Number.isInteger(code) && code >= 0 && code <= 16;
    assert.deepEqual(Object.keys(event).sort(), valid ? ["category", "elapsed_ms", "grpcCode"] : ["category", "elapsed_ms"]);
    assert.equal(event.grpcCode, valid ? code : undefined);
    assert.doesNotMatch(JSON.stringify(event), /provider-private|PRIVATE_PROVIDER|15551234567|test-only|aaaaaaaa|12345678/);
  });
}

test("native SDK grpcCode is retained without exposing its string code or private context", async (t) => {
  const state = await fixture(t);
  const options = { ...state.options, sendWelcome: async () => {
    throw Object.assign(new Error(`PRIVATE_PROVIDER_DETAILS ${phone}`), {
      code: "unauthenticated", grpcCode: 16, context: { phone, projectSecret },
    });
  } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.diagnostics[0]!.grpcCode, 16);
  assert.doesNotMatch(JSON.stringify(state.diagnostics), /unauthenticated|PRIVATE_PROVIDER|15551234567|test-only/);
});

test("an error code getter cannot leak text or break diagnostic handling", async (t) => {
  const state = await fixture(t);
  const failure = Object.defineProperty(new Error("PRIVATE_PROVIDER_DETAILS"), "code", {
    get() { throw new Error("PRIVATE_PROVIDER_DETAILS"); },
  });
  const options = { ...state.options, sendWelcome: async () => { throw failure; } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.diagnostics[0]!.category, "sdk_exception");
  assert.equal(state.diagnostics[0]!.grpcCode, undefined);
});

test("default console diagnostic contains only the fixed safe fields", async (t) => {
  const state = await fixture(t);
  const logged: unknown[][] = [];
  t.mock.method(console, "warn", (...values: unknown[]) => { logged.push(values); });
  const options = { ...state.options, diagnostic: undefined, sendWelcome: async () => {
    throw Object.assign(new Error(`PRIVATE_PROVIDER_DETAILS ${phone} ${projectSecret}`), { code: 7, userId, projectId });
  } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(logged.length, 1);
  assert.equal(logged[0]![0], "Photon welcome diagnostic:");
  const event = JSON.parse(String(logged[0]![1]));
  assert.deepEqual(Object.keys(event).sort(), ["category", "elapsed_ms", "grpcCode"]);
  assert.equal(event.category, "sdk_exception");
  assert.equal(event.grpcCode, 7);
  assert.doesNotMatch(JSON.stringify(logged), /PRIVATE_PROVIDER|15551234567|test-only|aaaaaaaa|12345678/);
});

for (const asynchronous of [false, true]) {
  test(`${asynchronous ? "async" : "sync"} diagnostic failure cannot break signup or cause resend`, async (t) => {
    const state = await fixture(t);
    let sends = 0;
    const options = { ...state.options, diagnostic: () => {
      if (asynchronous) return Promise.reject(new Error("PRIVATE_DIAGNOSTIC_FAILURE"));
      throw new Error("PRIVATE_DIAGNOSTIC_FAILURE");
    }, sendWelcome: async () => { sends++; throw new Error("PRIVATE_PROVIDER_DETAILS"); } };
    assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
    assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(sends, 1);
  });
}

test("late SDK success after the deadline stays uncertain and never sends again", async (t) => {
  const state = await fixture(t);
  let resolveSend!: (value: { id: string }) => void;
  let sends = 0;
  const options = { ...state.options, sendTimeoutMs: 8, sendWelcome: async () => {
    sends++;
    return new Promise<{ id: string }>((resolve) => { resolveSend = resolve; });
  } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.diagnostics[0]!.category, "send_deadline");
  assert.ok(state.diagnostics[0]!.elapsed_ms >= 7);
  resolveSend({ id: "late-provider-accepted-id" });
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(sends, 1);
  assert.equal(state.diagnostics.length, 1);
});

test("cached uncertainty and existing user without a receipt produce no welcome-attempt diagnostic", async (t) => {
  const state = await fixture(t);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  assert.equal((await createEnrollmentHandler(state.options)(input, key)).welcome_status, "uncertain");
  assert.equal((await createEnrollmentHandler(state.options)(input, key)).welcome_status, "uncertain");
  assert.deepEqual(state.diagnostics, []);
  assert.equal(state.sends.length, 0);
});

test("accepted-receipt storage failure is diagnosed separately without a duplicate welcome", async (t) => {
  const state = await fixture(t);
  let sends = 0;
  const path = join(state.directory, ledgerName());
  const options = { ...state.options, sendWelcome: async () => {
    sends++;
    // Simulate a receipt target becoming unwritable after the pending marker.
    await rm(path);
    await mkdir(path);
    return { id: "accepted-before-storage-failed" };
  } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).welcome_status, "uncertain");
  assert.equal(state.diagnostics.length, 1);
  assert.equal(state.diagnostics[0]!.category, "receipt_storage_failed");
  assert.equal(state.diagnostics[0]!.grpcCode, undefined);
  await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(503, "ENROLLMENT_STORAGE_UNAVAILABLE"));
  assert.equal(sends, 1);
});

test("stored pending after a crash checks active provider before returning uncertainty", async (t) => {
  const state = await fixture(t);
  state.setListed({ succeed: true, data: { users: [user], total: 1 } });
  await writeFile(join(state.directory, ledgerName()), JSON.stringify({ version: 1, state: "pending", assigned_phone_number: assigned }));
  const result = await createEnrollmentHandler(state.options)(input, key);
  assert.equal(result.welcome_status, "uncertain");
  assert.equal(state.calls.length, 1);
  assert.equal(state.sends.length, 0);
});

test("corrupt durable state fails closed instead of risking a resend", async (t) => {
  const state = await fixture(t);
  await writeFile(join(state.directory, legacyLedgerName), "provider-private-detail invalid-json");
  await assert.rejects(createEnrollmentHandler(state.options)(input, key), hasError(503, "ENROLLMENT_STORAGE_UNAVAILABLE"));
  assert.equal(state.calls.length, 0);
  assert.equal(state.sends.length, 0);
});

for (const value of [null, [], {}, { ...input, consent: false }, { ...input, consent: "true" },
  { ...input, phone_number: "5551234567" }, { ...input, phone_number: "+123456" },
  { ...input, phone_number: "+012345678" }, { ...input, phone_number: "+15551234567\n" },
  { ...input, secret: "unwanted" }]) {
  test(`invalid input ${JSON.stringify(value)} never reaches Photon`, async (t) => {
    const state = await fixture(t);
    await assert.rejects(createEnrollmentHandler(state.options)(value, key), hasError(400, "INVALID_ENROLLMENT_INPUT"));
    assert.equal(state.calls.length, 0);
    assert.equal(state.sends.length, 0);
  });
}

test("mismatched idempotency phone key is rejected before provider call", async (t) => {
  const state = await fixture(t);
  await assert.rejects(createEnrollmentHandler(state.options)(input, "enroll_wrong"), hasError(400, "INVALID_IDEMPOTENCY_KEY"));
  assert.equal(state.calls.length, 0);
});

test("official seven-digit minimum international number is accepted", async (t) => {
  const state = await fixture(t);
  const shortPhone = "+1234567";
  state.setListed({ succeed: true, data: { users: [{ ...user, phoneNumber: shortPhone }], total: 1 } });
  const shortKey = `enroll_${createHash("sha256").update(shortPhone).digest("hex")}`;
  const result = await createEnrollmentHandler(state.options)({ phone_number: shortPhone, consent: true }, shortKey);
  assert.equal(result.already_enrolled, true);
  assert.equal(result.welcome_status, "uncertain");
});

for (const payload of [{}, { succeed: false, message: "provider-private-detail" },
  { succeed: true, data: [] }, { succeed: true, data: { users: "wrong" } },
  { succeed: true, data: { users: [user, user] } }]) {
  test("malformed lookup response never enrolls or sends", async (t) => {
    const state = await fixture(t);
    state.setListed(payload);
    await assert.rejects(createEnrollmentHandler(state.options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
    assert.equal(state.calls.length, 1);
    assert.equal(state.sends.length, 0);
  });
}

for (const value of [{ ...user, type: "dedicated" }, { ...user, phoneNumber: "+15559876543" },
  { ...user, id: undefined }, { ...user, id: "not-a-provider-user-id" },
  { ...user, assignedPhoneNumber: "javascript:alert(1)" }, { ...user, assignedPhoneNumber: "" }]) {
  test("invalid created user does not trigger a welcome", async (t) => {
    const state = await fixture(t);
    state.setCreated({ succeed: true, data: value });
    await assert.rejects(createEnrollmentHandler(state.options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
    assert.equal(state.sends.length, 0);
    assert.deepEqual(await readdir(state.directory), []);
  });
}

for (const status of [401, 403, 422, 500]) {
  test(`provider status ${status} is sanitized without automatic retry`, async (t) => {
    const state = await fixture(t);
    let requests = 0;
    const options = { ...state.options, fetch: (async () => {
      requests++;
      return json({ message: "provider-private-detail test-only-project-secret" }, status);
    }) as typeof fetch };
    await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
    assert.equal(requests, 1);
    assert.equal(state.sends.length, 0);
  });
}

test("capacity or conflict stops with no upgrade request and no welcome", async (t) => {
  const state = await fixture(t);
  const options = { ...state.options, fetch: (async () => json({ message: "provider-private-detail" }, 409)) as typeof fetch };
  await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(409, "ENROLLMENT_CAPACITY_OR_CONFLICT"));
  assert.equal(state.sends.length, 0);
});

for (const mode of ["network", "html", "oversize", "invalid-json"] as const) {
  test(`${mode} enrollment response fails safely`, async (t) => {
    const state = await fixture(t);
    let calls = 0;
    const options = { ...state.options, fetch: (async () => {
      calls++;
      if (mode === "network") throw new Error("provider-private-detail test-only-project-secret");
      return new Response(mode === "oversize" ? "x".repeat(32769) : "provider-private-detail invalid-json", {
        headers: { "Content-Type": mode === "html" ? "text/html" : "application/json" },
      });
    }) as typeof fetch };
    await assert.rejects(createEnrollmentHandler(options)(input, key), hasError(503, "ENROLLMENT_UNAVAILABLE"));
    assert.equal(calls, 1);
    assert.equal(state.sends.length, 0);
  });
}

test("a project change cannot reuse another project's welcome ledger", async (t) => {
  const state = await fixture(t);
  await createEnrollmentHandler(state.options)(input, key);
  state.setListed({ succeed: true, data: { users: [], total: 0 } });
  let sends = 0;
  const options = { ...state.options, projectId: "12345678-1234-4123-8123-123456789abd",
    sendWelcome: async () => { sends++; return { id: "other-project-welcome" }; } };
  assert.equal((await createEnrollmentHandler(options)(input, key)).already_enrolled, false);
  assert.equal(sends, 1);
  assert.equal((await readdir(state.directory)).length, 2);
});

test("private /enroll retains bearer auth, JSON validation and idempotency", async (t) => {
  const state = await fixture(t);
  const token = "test-private-token";
  const server = createBridgeServer({ token, enroll: createEnrollmentHandler(state.options),
    sendSummary: async () => { throw new Error("Summary handler must not receive enrollment"); } });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  const { port } = server.address() as AddressInfo;
  async function call(headers: Record<string, string>, body: string) {
    return new Promise<{ status: number; body: unknown }>((resolve, reject) => {
      const req = request({ host: "127.0.0.1", port, method: "POST", path: "/enroll", headers }, (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => resolve({ status: response.statusCode!, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
      });
      req.on("error", reject);
      req.end(body);
    });
  }
  const headers = { authorization: `Bearer ${token}`, "content-type": "application/json", "idempotency-key": key };
  assert.equal((await call({ ...headers, authorization: "Bearer wrong" }, JSON.stringify(input))).status, 401);
  assert.equal((await call({ ...headers, "content-type": "text/plain" }, JSON.stringify(input))).status, 415);
  assert.equal((await call(headers, "invalid-json")).status, 400);
  assert.equal(state.calls.length, 0);
  assert.equal((await call(headers, JSON.stringify(input))).status, 200);
  assert.equal(state.sends.length, 1);
  assert.equal((await call(headers, JSON.stringify(input))).status, 200);
  assert.equal(state.sends.length, 1);
});
