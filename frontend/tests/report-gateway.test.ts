import assert from "node:assert/strict";
import { createServer, request, type Server } from "node:http";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { createGateway } from "../scripts/report-gateway.mjs";

async function listen(server: Server, t: TestContext): Promise<string> {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve, reject) => {
    server.closeAllConnections();
    server.close((error) => error ? reject(error) : resolve());
  }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return `http://127.0.0.1:${address.port}`;
}

async function call(origin: string, path: string, method = "GET", headers: Record<string, string> = {}) {
  return await new Promise<{ status: number; headers: import("node:http").IncomingHttpHeaders; body: string }>((resolve, reject) => {
    const outgoing = request(origin, { path, method, headers }, (response) => {
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => resolve({ status: response.statusCode!, headers: response.headers, body: Buffer.concat(chunks).toString() }));
      response.on("error", reject);
    });
    outgoing.on("error", reject);
    outgoing.end();
  });
}

async function setup(t: TestContext, readAllowedReports: () => Promise<unknown> = async () => ["report_public"]) {
  const calls: Array<{ path: string; method: string; headers: import("node:http").IncomingHttpHeaders }> = [];
  const upstream = await listen(createServer((incoming, response) => {
    calls.push({ path: incoming.url!, method: incoming.method!, headers: incoming.headers });
    response.setHeader("Set-Cookie", "private=session");
    response.setHeader("X-Internal-Key", "do-not-forward");
    if (incoming.url === "/reports/report_public") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ schema_version: "2.1", submitted: { post: { media: { media_id: "media_public" } } }, source: null,
        context_video: { media_id: "context_public" } }));
    } else if (incoming.url === "/reports/report_public/display") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ confidence_note: "Evidence, not a probability." }));
    } else if (incoming.url === "/reports/report_public/diagnostics") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ job_id: "public", capture_status: "recorded", events: [], terminal_output: "Per-job output" }));
    } else if (incoming.url === "/media/media_public" || incoming.url === "/media/context_public") {
      const data = "0123456789";
      response.setHeader("Content-Type", "video/mp4");
      if (incoming.headers.range === "bytes=2-4") {
        response.writeHead(206, { "Content-Range": "bytes 2-4/10", "Accept-Ranges": "bytes" });
        response.end(data.slice(2, 5));
      } else response.end(data);
    } else response.end("frontend asset");
  }), t);
  const origin = await listen(createGateway({ frontendOrigin: upstream, backendOrigin: upstream, readAllowedReports }), t);
  return { origin, calls };
}

test("gateway serves only selected report, display and diagnostic routes; strips private response headers", async (t) => {
  const { origin, calls } = await setup(t);
  for (const path of ["/reports/report_public", "/api/reports/report_public", "/api/reports/report_public/display", "/api/reports/report_public/diagnostics"]) {
    const response = await call(origin, path, "GET", { Cookie: "private=browser", Authorization: "Bearer browser-secret" });
    assert.equal(response.status, 200);
    assert.equal(response.headers["cache-control"], "no-store");
    assert.equal(response.headers["set-cookie"], undefined);
    assert.equal(response.headers["x-internal-key"], undefined);
  }
  assert.deepEqual(calls.map((item) => item.path), ["/reports/report_public", "/reports/report_public", "/reports/report_public/display", "/reports/report_public/diagnostics"]);
  assert.ok(calls.every((item) => !item.headers.cookie && !item.headers.authorization));
  const head = await call(origin, "/api/reports/report_public", "HEAD");
  assert.equal(head.status, 200);
  assert.equal(head.body, "");
});

test("unlisted reports, queries, mutations, backend routes and traversal fail closed", async (t) => {
  const { origin, calls } = await setup(t);
  for (const path of ["/", "/docs", "/api/ingest/job_1", "/api/reports/report_private", "/reports/report_private", "/reports/report_public/display",
    "/api/reports/report_public?debug=1", "/api/reports/report_private/diagnostics", "/reports/report_public/diagnostics", "/api/reports/report_public/diagnostics?debug=1", "/reports/report_public/../report_private", "/reports/%72eport_public", "/_next/static/../../.env",
    "/_next/static/%2e%2e/.env", "/_next/static/\\private", "//reports/report_public", "/api/media/media_private"]) {
    assert.equal((await call(origin, path)).status, 404, path);
  }
  for (const method of ["POST", "PUT", "DELETE", "OPTIONS"]) {
    const response = await call(origin, "/api/reports/report_public", method);
    assert.equal(response.status, 405);
    assert.equal(response.headers.allow, "GET, HEAD");
  }
  assert.ok(calls.every((item) => item.path === "/reports/report_public"));
});

test("missing or malformed sharing allowlists never expose reports or media", async (t) => {
  for (const readAllowedReports of [async () => { throw new Error("missing"); }, async () => null,
    async () => ({ reports: ["report_public"] }), async () => ["report_public", "../../private"]]) {
    const { origin, calls } = await setup(t, readAllowedReports);
    assert.equal((await call(origin, "/api/reports/report_public")).status, 404);
    assert.equal((await call(origin, "/api/media/media_public")).status, 404);
    assert.equal(calls.length, 0);
  }
});

test("media access follows selected report IDs and preserves a byte-range stream", async (t) => {
  let ids = ["report_public"];
  const { origin, calls } = await setup(t, async () => ids);
  const response = await call(origin, "/api/media/media_public", "GET", { Range: "bytes=2-4" });
  assert.equal(response.status, 206);
  assert.equal(response.headers["content-range"], "bytes 2-4/10");
  assert.equal(response.body, "234");
  assert.equal(calls.at(-1)?.headers.range, "bytes=2-4");
  const context = await call(origin, "/api/media/context_public", "GET", { Range: "bytes=2-4" });
  assert.equal(context.status, 206);
  assert.equal(context.body, "234");
  assert.equal((await call(origin, "/api/media/media_public", "GET", { Range: "bytes=0-1,3-4" })).status, 416);
  ids = [];
  assert.equal((await call(origin, "/api/media/media_public")).status, 404);
  assert.equal((await call(origin, "/api/media/context_public")).status, 404);
});

test("only fixed safe Next static asset paths reach the frontend", async (t) => {
  const { origin, calls } = await setup(t);
  assert.equal((await call(origin, "/_next/static/chunks/app/report-a1b2.js")).status, 200);
  assert.equal((await call(origin, "/favicon.ico")).status, 200);
  assert.equal((await call(origin, "/logo.png")).status, 200);
  assert.equal((await call(origin, "/logo-light.png")).status, 200);
  assert.equal((await call(origin, "/_next/webpack-hmr")).status, 404);
  assert.equal((await call(origin, "/_next/static/.env")).status, 404);
  assert.deepEqual(calls.map((item) => item.path), ["/_next/static/chunks/app/report-a1b2.js", "/favicon.ico", "/logo.png", "/logo-light.png"]);
});

test("Next dynamic-route assets allow bracket encoding only, never traversal or double encoding", async (t) => {
  const { origin, calls } = await setup(t);
  const asset = "/_next/static/chunks/app/reports/%5Breport_id%5D/page-e187b1a0d86fd36f.js";
  assert.equal((await call(origin, asset)).status, 200);
  assert.equal(calls[0]?.path, asset);
  for (const path of ["/_next/static/chunks/%2e%2e/private.js", "/_next/static/chunks/%252e%252e/private.js",
    "/_next/static/chunks/%255Breport_id%255D/page.js", "/_next/static/chunks/%2Fprivate.js",
    "/_next/static/chunks/%5B..%5D/page.js", "/_next/static/chunks/%5Breport_id%5D/../private.js",
    "/reports/%5Breport_id%5D", "/_next/static/chunks/%5Breport_id%5D/page.js?secret=1"]) {
    assert.equal((await call(origin, path)).status, 404, path);
  }
  assert.equal(calls.length, 1);
});

test("default file allowlist reloads after publication and revocation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "quantro-sharing-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const allowlistFile = join(directory, "reports.json");
  const upstream = await listen(createServer((_request, response) => response.end("shared report")), t);
  const origin = await listen(createGateway({ frontendOrigin: upstream, backendOrigin: upstream, allowlistFile }), t);
  assert.equal((await call(origin, "/reports/report_public")).status, 404);
  await writeFile(allowlistFile, JSON.stringify(["report_public"]));
  assert.equal((await call(origin, "/reports/report_public")).status, 200);
  await writeFile(allowlistFile, JSON.stringify([]));
  assert.equal((await call(origin, "/reports/report_public")).status, 404);
});

test("gateway cannot be configured as an external or credentialed open proxy", () => {
  for (const backendOrigin of ["https://example.com", "http://localhost:8000", "http://127.0.0.1:8000/private", "http://name:password@127.0.0.1:8000"]) {
    assert.throws(() => createGateway({ backendOrigin }), /fixed loopback/);
  }
});

test("compact progress uses the existing public report allowlist", async (t) => {
  const {origin,calls}=await setup(t);
  assert.equal((await call(origin,"/api/reports/report_public/progress")).status,200);
  assert.equal((await call(origin,"/api/reports/report_private/progress")).status,404);
  assert.equal((await call(origin,"/api/reports/report_public/progress?debug=1")).status,404);
  assert.equal((await call(origin,"/api/reports/report_public/progress","POST")).status,405);
  assert.ok(calls.some(item=>item.path==="/reports/report_public/progress"));
  assert.ok(calls.every(item=>!item.headers.cookie&&!item.headers.authorization));
});
