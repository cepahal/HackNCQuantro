import assert from "node:assert/strict";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import test, { type TestContext } from "node:test";
import { createBridgeServer } from "../src/server.ts";
import { BridgeError } from "../src/summary.ts";

const token = "local-test-bridge-token";
const input = {
  conversation_id: "conversation_001",
  summary: "No verified source was found.",
  report_url: "https://quantro.example/reports/report_001",
};
const accepted = { conversation_id: "conversation_001", message_id: "message_001", status: "accepted" as const };
const headers = {
  authorization: `Bearer ${token}`,
  "content-type": "application/json",
  "idempotency-key": "job_001",
};

async function fixture(t: TestContext, sendSummary: Parameters<typeof createBridgeServer>[0]["sendSummary"] = async () => accepted) {
  const server = createBridgeServer({ token, sendSummary });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  const { port } = server.address() as AddressInfo;

  function call(method: string, path: string, requestHeaders: Record<string, string> = {}, body?: string | Buffer[]) {
    return new Promise<{ status: number; headers: Record<string, unknown>; body: string }>((resolve, reject) => {
      const req = request({ host: "127.0.0.1", port, method, path, headers: requestHeaders }, (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.once("end", () => resolve({ status: response.statusCode!, headers: response.headers, body: Buffer.concat(chunks).toString("utf8") }));
        response.once("error", reject);
      });
      req.once("error", reject);
      if (Array.isArray(body)) {
        for (const chunk of body) req.write(chunk);
        req.end();
      } else {
        req.end(body);
      }
    });
  }
  return { server, call };
}

test("health is public, reveals no configuration, and configures bounded request timeouts", async (t) => {
  const { server, call } = await fixture(t);
  const response = await call("GET", "/health");
  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(response.body), { status: "ok", service: "quantro-photon" });
  assert.equal(server.requestTimeout, 10_000);
  assert.equal(server.headersTimeout, 10_000);
  assert.equal(response.headers["access-control-allow-origin"], undefined);
  assert.equal(response.headers["cache-control"], "no-store");
  assert.doesNotMatch(response.body, /local-test-bridge-token/);
});

test("missing and incorrect bearer tokens never call the sender", async (t) => {
  let calls = 0;
  const { call } = await fixture(t, async () => { calls++; return accepted; });
  for (const authorization of ["", "Bearer wrong", `Bearer ${"x".repeat(token.length)}`]) {
    const response = await call("POST", "/send-summary", { ...headers, authorization }, JSON.stringify(input));
    assert.equal(response.status, 401);
    assert.equal(JSON.parse(response.body).error.code, "UNAUTHORIZED");
    assert.doesNotMatch(response.body, /local-test-bridge-token/);
  }
  assert.equal(calls, 0);
});

test("valid requests forward JSON and the idempotency job ID without claiming delivery", async (t) => {
  const calls: { input: unknown; jobId: string }[] = [];
  const { call } = await fixture(t, async (value, jobId) => { calls.push({ input: value, jobId }); return accepted; });
  const response = await call("POST", "/send-summary", { ...headers, "content-type": "application/json; charset=utf-8" }, JSON.stringify(input));
  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(response.body), accepted);
  assert.deepEqual(calls, [{ input, jobId: "job_001" }]);
});

test("unsupported content types and missing or invalid idempotency keys are rejected", async (t) => {
  let calls = 0;
  const { call } = await fixture(t, async () => { calls++; return accepted; });
  for (const contentType of ["text/plain", "application/javascript", ""]) {
    const response = await call("POST", "/send-summary", { ...headers, "content-type": contentType }, JSON.stringify(input));
    assert.equal(response.status, 415);
  }
  for (const jobId of ["", "job/001", "job 001", "x".repeat(129)]) {
    const response = await call("POST", "/send-summary", { ...headers, "idempotency-key": jobId }, JSON.stringify(input));
    assert.equal(response.status, 400);
    assert.equal(JSON.parse(response.body).error.code, "INVALID_IDEMPOTENCY_KEY");
  }
  assert.equal(calls, 0);
});

test("invalid JSON returns a safe error without echoing input", async (t) => {
  let calls = 0;
  const { call } = await fixture(t, async () => { calls++; return accepted; });
  const response = await call("POST", "/send-summary", headers, "private-user-input not-json");
  assert.equal(response.status, 400);
  assert.equal(JSON.parse(response.body).error.code, "INVALID_JSON");
  assert.doesNotMatch(response.body, /private-user-input/);
  assert.equal(calls, 0);
});

test("streamed chunked bodies are bounded without Content-Length", async (t) => {
  let calls = 0;
  const { call } = await fixture(t, async () => { calls++; return accepted; });
  const response = await call("POST", "/send-summary", headers, [Buffer.alloc(8_192, "x"), Buffer.alloc(8_192, "x"), Buffer.from("x")]);
  assert.equal(response.status, 413);
  assert.equal(JSON.parse(response.body).error.code, "REQUEST_TOO_LARGE");
  assert.equal(calls, 0);
});

test("a disconnected client cannot crash the bridge while streaming a body", async (t) => {
  let calls = 0;
  const { server, call } = await fixture(t, async () => { calls++; return accepted; });
  const { port } = server.address() as AddressInfo;
  let incomingClosed: Promise<void>;
  const incomingStarted = new Promise<void>((resolve) => {
    server.once("request", (incoming) => {
      incomingClosed = new Promise((closed) => incoming.once("close", closed));
      resolve();
    });
  });
  const client = request({ host: "127.0.0.1", port, method: "POST", path: "/send-summary", headers });
  client.on("error", () => {});
  client.write("{");
  await incomingStarted;
  client.destroy();
  await incomingClosed!;
  assert.equal(calls, 0);
  assert.equal((await call("GET", "/health")).status, 200);
});

test("expected summary errors retain the shared contract", async (t) => {
  const failure = new BridgeError(503, "MESSAGE_SEND_FAILED", "Photon messaging is unavailable.", true);
  const { call } = await fixture(t, async () => { throw failure; });
  const response = await call("POST", "/send-summary", headers, JSON.stringify(input));
  assert.equal(response.status, 503);
  assert.deepEqual(JSON.parse(response.body), failure.body);
});

test("unknown sender errors cannot expose credentials or internal responses", async (t) => {
  const { call } = await fixture(t, async () => { throw new Error("secret-provider-key raw-internal-response"); });
  const response = await call("POST", "/send-summary", headers, JSON.stringify(input));
  assert.equal(response.status, 500);
  assert.equal(JSON.parse(response.body).error.code, "MESSAGE_SEND_FAILED");
  assert.equal(JSON.parse(response.body).error.retryable, false);
  assert.doesNotMatch(response.body, /secret-provider-key|raw-internal-response/);
});

test("unknown routes and methods return 404", async (t) => {
  const { call } = await fixture(t);
  for (const [method, path] of [["GET", "/send-summary"], ["POST", "/health"], ["GET", "/unknown"]]) {
    const response = await call(method!, path!);
    assert.equal(response.status, 404);
  }
});

test("empty authentication tokens are refused before the server starts", () => {
  assert.throws(() => createBridgeServer({ token: "  ", sendSummary: async () => accepted }), /authentication token is required/);
});
