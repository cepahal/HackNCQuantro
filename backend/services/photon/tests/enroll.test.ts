import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { createEnrollmentHandler, EnrollmentError, WELCOME_MESSAGE } from "../src/enroll.ts";
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

async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "quantro-enroll-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const calls: { url: string; options: RequestInit }[] = [];
  const sends: { recipient: string; text: string }[] = [];
  let listed: unknown = { succeed: true, data: { users: [], total: 0 } };
  let created: unknown = { succeed: true, data: user };
  const options = {
    projectId, projectSecret, deliveryDirectory: directory,
    fetch: (async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), options: init! });
      if (init?.method === "GET") return json(listed);
      const createdUser = (created as { data?: unknown })?.data;
      if (createdUser) listed = { succeed: true, data: { users: [createdUser], total: 1 } };
      return json(created);
    }) as typeof fetch,
    sendWelcome: async (recipient: string, text: string) => {
      const persisted = JSON.parse(await readFile(join(directory, ledgerName()), "utf8"));
      assert.equal(persisted.state, "pending");
      sends.push({ recipient, text });
      return { id: "welcome-message-id" };
    },
  };
  return { directory, calls, sends, options,
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
  assert.equal(result.welcome_status, "accepted");
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
    assert.equal(JSON.parse(await readFile(join(state.directory, ledgerName()), "utf8")).state, "uncertain");
  });
}

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
