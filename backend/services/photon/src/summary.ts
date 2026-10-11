import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { resolve } from "node:path";

export interface SummaryInput {
  conversation_id: string;
  summary: string;
  report_url: string;
}

export interface SummaryAccepted {
  conversation_id: string;
  message_id: string;
  status: "accepted";
}

export interface SummaryDiagnostic {
  category: "sdk_exception" | "send_deadline" | "invalid_message_id" | "receipt_storage_failed"
    | "conversation_resolution_failed" | "report_preparation_failed";
  elapsed_ms: number;
  grpcCode?: number;
}

class SummaryDeadlineError extends Error {}
class SummaryMessageIdError extends Error {}

export class BridgeError extends Error {
  readonly status: number;
  readonly body: {
    error: {
      code: string;
      stage: "send_summary";
      message: string;
      retryable: boolean;
    };
  };

  constructor(status: number, code: string, message: string, retryable = false) {
    super(message);
    this.name = "BridgeError";
    this.status = status;
    this.body = { error: { code, stage: "send_summary", message, retryable } };
  }
}

interface SummarySenderOptions {
  resolveConversation: (conversationId: string) => Promise<{
    send(text: string): Promise<{ id: string } | undefined>;
  }>;
  deliveryDirectory: string;
  sendTimeoutMs?: number;
  prepareReport?: (input: SummaryInput, jobId: string) => Promise<void>;
  diagnostic?: (event: SummaryDiagnostic) => void;
}

interface DeliveryRecord {
  version: 1;
  fingerprint: string;
  state: "pending" | "failed" | "accepted";
  message_id?: string;
  message_ids?: string[];
}

const digest = (value: string): string => createHash("sha256").update(value).digest("hex");
const invalidInput = (): BridgeError => new BridgeError(
  400, "INVALID_SUMMARY_INPUT", "Provide a conversation, a summary of 1–2000 characters, and an HTTP(S) report URL.",
);
const uncertainDelivery = (): BridgeError => new BridgeError(
  409, "DELIVERY_UNCERTAIN", "A delivery was already attempted for this job. Verify its outcome before requesting another send.",
);
const storageError = (): BridgeError => new BridgeError(
  503, "DELIVERY_STORAGE_UNAVAILABLE", "Delivery state could not be safely stored. No automatic resend is permitted.",
);

function validateInput(input: unknown, jobId: string): SummaryInput {
  if (typeof jobId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(jobId)) {
    throw new BridgeError(400, "INVALID_IDEMPOTENCY_KEY", "Provide an Idempotency-Key of 1–128 letters, digits, underscores, or hyphens.");
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) throw invalidInput();
  const record = input as Record<string, unknown>;
  const { conversation_id, summary, report_url } = record;
  if (
    typeof conversation_id !== "string" || conversation_id.length > 512 || !conversation_id.trim()
    || /[\u0000-\u001f\u007f]/.test(conversation_id)
    || typeof summary !== "string" || summary.length > 2000 || !summary.trim()
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(summary)
    || typeof report_url !== "string" || !report_url.trim() || report_url.length > 4096
    || /[\u0000-\u001f\u007f]/.test(report_url)
  ) throw invalidInput();
  let url: URL;
  try {
    url = new URL(report_url);
  } catch {
    throw invalidInput();
  }
  if (!(["https:", "http:"].includes(url.protocol)) || url.username || url.password) throw invalidInput();
  return { conversation_id, summary: summary.trim(), report_url: url.href };
}

function validMessageId(id: unknown): id is string {
  return typeof id === "string" && id.length <= 512 && id.trim().length > 0 && !/[\u0000-\u001f\u007f]/.test(id);
}

function plainText(value: string): string {
  return value.replace(/\*\*/g, "").replace(/\s*\u2014\s*/g, ", ").replace(/\s+/g, " ").trim();
}

function shortExplanation(value: string): string {
  const text = plainText(value);
  if (text.length <= 650) return text;
  const prefix = text.slice(0, 650);
  const end = [...prefix.matchAll(/[.!?](?=\s|$)/g)].at(-1)?.index;
  return end === undefined
    ? "The analysis is longer than a text. Open the report for the full explanation."
    : prefix.slice(0, end + 1);
}

/** Use supplied report wording; formatting never infers truth, intent, or authenticity. */
export function formatSummaryMessages(input: SummaryInput): readonly [string, string, string] {
  const parsed = input.summary.trim().match(/^Takeaways:\s*([\s\S]+?)\n\s*\nWhat this means:\s*([\s\S]+?)\n\s*\nReliability:\s*([\s\S]+)$/);
  const sections = parsed?.slice(1).every(section => section.trim()) ? parsed : null;
  const reliability = sections ? plainText(sections[3]!) : "Reliability is unconfirmed from this summary alone.";
  // Do not cut off a qualification in the reliability statement.
  const lead = reliability.length <= 240 ? reliability
    : "The evidence does not establish overall accuracy. Open the report for the assessment and its limits.";
  return [
    `TL;DR: ${lead}`,
    shortExplanation(sections ? `${sections[1]} ${sections[2]}` : input.summary),
    `Full report:\n${input.report_url}`,
  ];
}

function parseRecord(raw: string): DeliveryRecord {
  const record: unknown = JSON.parse(raw);
  if (typeof record !== "object" || record === null) throw storageError();
  const value = record as Record<string, unknown>;
  if (
    value.version !== 1 || typeof value.fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(value.fingerprint)
    || (value.state !== "pending" && value.state !== "failed" && value.state !== "accepted")
    || (value.state === "accepted" && !validMessageId(value.message_id))
    || (value.message_ids !== undefined && (!Array.isArray(value.message_ids)
      || value.message_ids.length > 3 || !value.message_ids.every(validMessageId)))
    || (value.state === "accepted" && Array.isArray(value.message_ids)
      && (value.message_ids.length !== 3 || value.message_ids[2] !== value.message_id))
  ) throw storageError();
  return {
    version: 1,
    fingerprint: value.fingerprint,
    state: value.state,
    ...(validMessageId(value.message_id) ? { message_id: value.message_id } : {}),
    ...(Array.isArray(value.message_ids) ? { message_ids: value.message_ids as string[] } : {}),
  };
}

async function writeRecord(path: string, record: DeliveryRecord): Promise<void> {
  const file = await open(path, "wx", 0o600);
  try {
    await file.writeFile(JSON.stringify(record), "utf8");
    await file.sync();
  } finally {
    await file.close();
  }
}

async function replaceRecord(path: string, record: DeliveryRecord): Promise<void> {
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeRecord(temporary, record);
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
}

async function withSendDeadline<T>(send: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      send,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new SummaryDeadlineError()), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export function createSummarySender(options: SummarySenderOptions) {
  const sendTimeoutMs = options.sendTimeoutMs ?? 12_000;
  if (!Number.isInteger(sendTimeoutMs) || sendTimeoutMs < 1 || sendTimeoutMs > 12_000) {
    throw new BridgeError(500, "INVALID_SEND_TIMEOUT", "The provider send deadline must be an integer from 1 to 12000 milliseconds.");
  }
  const directory = resolve(options.deliveryDirectory);
  const inFlight = new Map<string, { fingerprint: string; promise: Promise<SummaryAccepted> }>();

  function diagnose(category: SummaryDiagnostic["category"], started: number, error?: unknown): void {
    // Never log provider text, stack, identifiers, or arbitrary enumerable fields.
    const event: SummaryDiagnostic = { category, elapsed_ms: Math.max(0, Math.round(performance.now() - started)) };
    try {
      if (category === "sdk_exception" && typeof error === "object" && error !== null) {
        for (const key of ["grpcCode", "code"] as const) {
          const code = Object.getOwnPropertyDescriptor(error, key)?.value;
          if (typeof code === "number" && Number.isInteger(code) && code >= 0 && code <= 16) {
            event.grpcCode = code;
            break;
          }
        }
      }
    } catch { /* Untrusted getters/proxies cannot disrupt delivery or diagnostics. */ }
    try {
      if (options.diagnostic) {
        void Promise.resolve(options.diagnostic(event)).catch(() => undefined);
      } else {
        console.warn("Photon summary diagnostic:", JSON.stringify(event));
      }
    } catch { /* Diagnostics must never change delivery state or the bridge response. */ }
  }

  async function deliver(input: SummaryInput, jobId: string, key: string, fingerprint: string): Promise<SummaryAccepted> {
    const path = resolve(directory, `${key}.json`);
    let pending: DeliveryRecord = { version: 1, fingerprint, state: "pending" };
    try {
      await mkdir(directory, { recursive: true });
    } catch {
      throw storageError();
    }
    // The exclusive, synced marker must exist before any provider interaction.
    try {
      await writeRecord(path, pending);
    } catch (error) {
      if (!(error instanceof Error && "code" in error && error.code === "EEXIST")) throw storageError();
      let existing: DeliveryRecord;
      try {
        existing = parseRecord(await readFile(path, "utf8"));
      } catch {
        throw storageError();
      }
      if (existing.fingerprint !== fingerprint) {
        throw new BridgeError(409, "IDEMPOTENCY_CONFLICT", "This job and conversation already use a different summary payload.");
      }
      if (existing.state !== "accepted" || !validMessageId(existing.message_id)) throw uncertainDelivery();
      return { conversation_id: input.conversation_id, message_id: existing.message_id, status: "accepted" };
    }

    let conversation: Awaited<ReturnType<SummarySenderOptions["resolveConversation"]>>;
    const resolutionStarted = performance.now();
    try {
      conversation = await options.resolveConversation(input.conversation_id);
    } catch (error) {
      diagnose("conversation_resolution_failed", resolutionStarted);
      // This sender owns the exclusive marker, and no send has been attempted.
      try {
        await unlink(path);
      } catch {
        throw storageError();
      }
      if (error instanceof BridgeError) throw error;
      throw new BridgeError(503, "CONVERSATION_UNAVAILABLE", "The conversation could not be resolved. Retry when it becomes available.", true);
    }

    const preparationStarted = performance.now();
    try {
      await options.prepareReport?.(input, jobId);
    } catch {
      diagnose("report_preparation_failed", preparationStarted);
      // Preparation has not sent anything; allow a safe retry of this same job.
      try {
        await unlink(path);
      } catch {
        throw storageError();
      }
      throw storageError();
    }

    let messageId = "";
    const sendStarted = performance.now();
    // All three texts share the existing send deadline. An uncertain part stops
    // the sequence; neither accepted nor attempted parts may automatically replay.
    for (const message of formatSummaryMessages(input)) {
      try {
        const remainingMs = sendTimeoutMs - (performance.now() - sendStarted);
        if (remainingMs <= 0) throw new SummaryDeadlineError();
        const sent = await withSendDeadline(conversation.send(message), remainingMs);
        if (!validMessageId(sent?.id)) throw new SummaryMessageIdError();
        messageId = sent.id;
      } catch (error) {
        diagnose(error instanceof SummaryDeadlineError ? "send_deadline"
          : error instanceof SummaryMessageIdError ? "invalid_message_id" : "sdk_exception", sendStarted, error);
        await replaceRecord(path, { ...pending, state: "failed" }).catch(() => undefined);
        throw new BridgeError(502, "DELIVERY_UNCERTAIN", "Photon did not confirm all message IDs. Verify delivery before requesting another send.");
      }
      pending = { ...pending, message_ids: [...(pending.message_ids ?? []), messageId] };
      try {
        // Persist each provider acceptance before interacting with it again.
        await replaceRecord(path, pending);
      } catch {
        diagnose("receipt_storage_failed", sendStarted);
        throw storageError();
      }
    }
    try {
      await replaceRecord(path, { ...pending, state: "accepted", message_id: messageId });
    } catch {
      diagnose("receipt_storage_failed", sendStarted);
      throw storageError();
    }
    return { conversation_id: input.conversation_id, message_id: messageId, status: "accepted" };
  }

  return async function sendSummary(input: unknown, jobId: string): Promise<SummaryAccepted> {
    const validated = validateInput(input, jobId);
    const key = digest(JSON.stringify([validated.conversation_id, jobId]));
    const fingerprint = digest(JSON.stringify(validated));
    const active = inFlight.get(key);
    if (active) {
      if (active.fingerprint !== fingerprint) {
        throw new BridgeError(409, "IDEMPOTENCY_CONFLICT", "This job and conversation already use a different summary payload.");
      }
      return active.promise;
    }
    const promise = deliver(validated, jobId, key, fingerprint);
    inFlight.set(key, { fingerprint, promise });
    try {
      return await promise;
    } finally {
      inFlight.delete(key);
    }
  };
}
