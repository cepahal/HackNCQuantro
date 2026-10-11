/** Read-only sharing gateway. Keep the analysis API bound to the local machine. */
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

const REPORT_ID = /^report_[A-Za-z0-9_-]{1,128}$/;
const MEDIA_ID = /^[A-Za-z0-9_-]{1,128}$/;
const DEFAULT_ALLOWLIST = fileURLToPath(new URL("../../.data/photon/public-reports.json", import.meta.url));
const SAFE_RESPONSE_HEADERS = ["content-type", "content-range", "accept-ranges", "etag", "last-modified"];

function localOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.username || url.password
      || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Gateway upstreams must be fixed loopback HTTP origins.");
  }
  return url.origin;
}

async function allowedReports(readAllowedReports) {
  try {
    const values = await readAllowedReports();
    if (!Array.isArray(values) || values.length > 32 || !values.every((id) => typeof id === "string" && REPORT_ID.test(id))) {
      return new Set();
    }
    return new Set(values);
  } catch {
    return new Set();
  }
}

function safePath(value) {
  // Match the raw request target before URL normalization can hide traversal.
  return typeof value === "string" && value.length <= 2048 && value.startsWith("/")
    && !/[\\%?#\x00-\x20\x7f]/.test(value) && !value.includes("..");
}

function reply(response, status, message, method) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...(status === 405 ? { Allow: "GET, HEAD" } : {}),
  });
  response.end(method === "HEAD" ? undefined : JSON.stringify({ error: { message } }));
}

async function forward(request, response, origin, path, media = false) {
  const headers = { "Accept-Encoding": "identity" };
  if (media && request.headers.range) {
    if (!/^bytes=(?:\d+-\d*|-\d+)$/.test(request.headers.range)) {
      reply(response, 416, "Invalid media range.", request.method);
      return;
    }
    headers.Range = request.headers.range;
  }
  const upstream = await fetch(`${origin}${path}`, {
    method: request.method,
    headers,
    redirect: "manual",
    signal: AbortSignal.timeout(60_000),
  });
  if (upstream.status >= 300 && upstream.status < 400) {
    await upstream.body?.cancel();
    reply(response, 502, "The shared report service is unavailable.", request.method);
    return;
  }
  const outgoing = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
  };
  for (const name of SAFE_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) outgoing[name] = value;
  }
  response.writeHead(upstream.status, outgoing);
  if (request.method === "HEAD" || !upstream.body) {
    await upstream.body?.cancel();
    response.end();
    return;
  }
  await pipeline(Readable.fromWeb(upstream.body), response);
}

async function mediaIsShared(mediaId, reports, backendOrigin) {
  for (const reportId of reports) {
    const response = await fetch(`${backendOrigin}/reports/${reportId}`, {
      headers: { "Accept-Encoding": "identity" }, redirect: "manual", signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      await response.body?.cancel();
      continue;
    }
    const report = await response.json();
    if (report?.schema_version !== "2.1") continue;
    if ([report.submitted, report.source].some((item) => item?.post?.media?.media_id === mediaId)) return true;
    if (report.context_video?.media_id === mediaId) return true;
  }
  return false;
}

/**
 * @param {{frontendOrigin?: string, backendOrigin?: string, allowlistFile?: string,
 * readAllowedReports?: () => Promise<unknown>}} [options]
 */
export function createGateway(options = {}) {
  const frontendOrigin = localOrigin(options.frontendOrigin ?? "http://127.0.0.1:3000");
  const backendOrigin = localOrigin(options.backendOrigin ?? "http://127.0.0.1:8000");
  const allowlistFile = options.allowlistFile ?? DEFAULT_ALLOWLIST;
  const readAllowedReports = options.readAllowedReports ?? (async () => {
    if ((await stat(allowlistFile)).size > 16_384) throw new Error("Invalid sharing allowlist.");
    return JSON.parse(await readFile(allowlistFile, "utf8"));
  });
  return createServer(async (request, response) => {
    try {
      if (request.method !== "GET" && request.method !== "HEAD") {
        reply(response, 405, "This report gateway is read-only.", request.method);
        return;
      }
      const path = request.url;
      // Next emits bracket-encoded dynamic-route asset directories. Decode only
      // those two characters for validation; every other escape stays forbidden.
      const checkedPath = typeof path === "string" && path.startsWith("/_next/static/")
        ? path.replace(/%5B/gi, "[").replace(/%5D/gi, "]") : path;
      if (!safePath(checkedPath)) {
        reply(response, 404, "Shared report not found.", request.method);
        return;
      }
      if (["/favicon.ico", "/logo.png", "/logo-light.png"].includes(path) || /^\/_next\/static\/(?:(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*|\[[A-Za-z0-9_-]+\])\/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*$/.test(checkedPath)) {
        await forward(request, response, frontendOrigin, path);
        return;
      }
      const reports = await allowedReports(readAllowedReports);
      const reportRoute = /^\/(api\/)?reports\/(report_[A-Za-z0-9_-]{1,128})(\/(?:display|diagnostics|progress))?$/.exec(path);
      if (reportRoute && reports.has(reportRoute[2]) && (reportRoute[1] || !reportRoute[3])) {
        await forward(request, response, reportRoute[1] ? backendOrigin : frontendOrigin, path.replace(/^\/api\//, "/"));
        return;
      }
      const mediaRoute = /^\/api\/media\/([A-Za-z0-9_-]{1,128})$/.exec(path);
      if (mediaRoute && MEDIA_ID.test(mediaRoute[1]) && await mediaIsShared(mediaRoute[1], reports, backendOrigin)) {
        await forward(request, response, backendOrigin, `/media/${mediaRoute[1]}`, true);
        return;
      }
      reply(response, 404, "Shared report not found.", request.method);
    } catch {
      if (response.headersSent) response.destroy();
      else reply(response, 502, "The shared report service is unavailable.", request.method);
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  createGateway().listen(3100, "127.0.0.1", () => {
    console.log("Read-only report gateway ready at http://127.0.0.1:3100.");
  });
}
