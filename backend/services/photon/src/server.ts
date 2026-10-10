import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { BridgeError } from "./summary.ts";

const MAX_BODY_BYTES = 16 * 1_024;
const REQUEST_TIMEOUT_MS = 10_000;

type SummaryResult = {
  conversation_id: string;
  message_id: string;
  status: "accepted";
};

type BridgeOptions = {
  token: string;
  sendSummary: (input: unknown, jobId: string) => Promise<SummaryResult>;
};

function respond(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify(body));
}

function rejectRequest(request: IncomingMessage, response: ServerResponse, error: BridgeError): void {
  request.pause();
  response.setHeader("Connection", "close");
  response.once("finish", () => request.socket.destroySoon());
  respond(response, error.status, error.body);
}

function readJson(request: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let bytes = 0;
    const timer = setTimeout(() => {
      request.pause();
      cleanup();
      reject(new BridgeError(408, "REQUEST_TIMEOUT", "The request body timed out.", true));
    }, REQUEST_TIMEOUT_MS);
    timer.unref();

    function cleanup(): void {
      clearTimeout(timer);
      request.removeListener("data", onData);
      request.removeListener("end", onEnd);
      request.removeListener("error", onError);
      request.removeListener("aborted", onError);
    }

    function onData(chunk: Buffer): void {
      bytes += chunk.length;
      if (bytes > MAX_BODY_BYTES) {
        request.pause();
        cleanup();
        reject(new BridgeError(413, "REQUEST_TOO_LARGE", "The request body exceeds 16384 bytes."));
        return;
      }
      chunks.push(chunk);
    }

    function onEnd(): void {
      cleanup();
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown);
      } catch {
        reject(new BridgeError(400, "INVALID_JSON", "The request body must be valid JSON."));
      }
    }

    function onError(): void {
      cleanup();
      reject(new BridgeError(400, "INVALID_REQUEST", "The request body could not be read."));
    }

    request.on("data", onData);
    request.once("end", onEnd);
    request.once("error", onError);
    request.once("aborted", onError);
  });
}

export function createBridgeServer({ token, sendSummary }: BridgeOptions): Server {
  if (!token.trim()) throw new Error("A bridge authentication token is required.");
  const expectedAuthorization = Buffer.from(`Bearer ${token}`, "utf8");

  return createServer({ requestTimeout: REQUEST_TIMEOUT_MS, headersTimeout: REQUEST_TIMEOUT_MS }, (request, response) => {
    request.on("error", () => {});
    void (async () => {
      if (request.method === "GET" && request.url === "/health") {
        respond(response, 200, { status: "ok", service: "quantro-photon" });
        return;
      }
      if (request.method !== "POST" || request.url !== "/send-summary") {
        rejectRequest(request, response, new BridgeError(404, "NOT_FOUND", "The requested route was not found."));
        return;
      }

      const authorization = Buffer.from(request.headers.authorization ?? "", "utf8");
      if (authorization.length !== expectedAuthorization.length || !timingSafeEqual(authorization, expectedAuthorization)) {
        rejectRequest(request, response, new BridgeError(401, "UNAUTHORIZED", "A valid bridge authentication token is required."));
        return;
      }
      const contentType = request.headers["content-type"]?.split(";", 1)[0]?.trim().toLowerCase();
      if (contentType !== "application/json") {
        rejectRequest(request, response, new BridgeError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json."));
        return;
      }
      const jobId = request.headers["idempotency-key"];
      if (typeof jobId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(jobId)) {
        rejectRequest(request, response, new BridgeError(400, "INVALID_IDEMPOTENCY_KEY", "A valid Idempotency-Key header is required."));
        return;
      }

      try {
        const input = await readJson(request);
        const result = await sendSummary(input, jobId);
        respond(response, 200, result);
      } catch (error) {
        const safeError = error instanceof BridgeError && Number.isInteger(error.status) && error.status >= 400 && error.status <= 599
          ? error
          : new BridgeError(500, "MESSAGE_SEND_FAILED", "The message could not be sent.");
        if (!response.destroyed) rejectRequest(request, response, safeError);
      }
    })();
  });
}
