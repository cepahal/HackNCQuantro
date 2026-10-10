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
}

interface DeliveryRecord {
  version: 1;
  fingerprint: string;
  state: "pending" | "failed" | "accepted";
  message_id?: string;
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

function parseRecord(raw: string): DeliveryRecord {
  const record: unknown = JSON.parse(raw);
  if (typeof record !== "object" || record === null) throw storageError();
  const value = record as Record<string, unknown>;
  if (
    value.version !== 1 || typeof value.fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(value.fingerprint)
    || (value.state !== "pending" && value.state !== "failed" && value.state !== "accepted")
    || (value.state === "accepted" && !validMessageId(value.message_id))
  ) throw storageError();
  return {
    version: 1,
    fingerprint: value.fingerprint,
    state: value.state,
    ...(validMessageId(value.message_id) ? { message_id: value.message_id } : {}),
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

export function createSummarySender(options: SummarySenderOptions) {
  const directory = resolve(options.deliveryDirectory);
  const inFlight = new Map<string, { fingerprint: string; promise: Promise<SummaryAccepted> }>();

  async function deliver(input: SummaryInput, key: string, fingerprint: string): Promise<SummaryAccepted> {
    const path = resolve(directory, `${key}.json`);
    const pending: DeliveryRecord = { version: 1, fingerprint, state: "pending" };
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
    try {
      conversation = await options.resolveConversation(input.conversation_id);
    } catch (error) {
      // This sender owns the exclusive marker, and no send has been attempted.
      try {
        await unlink(path);
      } catch {
        throw storageError();
      }
      if (error instanceof BridgeError) throw error;
      throw new BridgeError(503, "CONVERSATION_UNAVAILABLE", "The conversation could not be resolved. Retry when it becomes available.", true);
    }

    let messageId: string;
    try {
      const sent = await conversation.send(`${input.summary}\n\nFull report: ${input.report_url}`);
      if (!validMessageId(sent?.id)) throw uncertainDelivery();
      messageId = sent.id;
    } catch {
      await replaceRecord(path, { ...pending, state: "failed" }).catch(() => undefined);
      throw new BridgeError(502, "DELIVERY_UNCERTAIN", "Photon did not confirm a message ID. Verify delivery before requesting another send.");
    }
    try {
      await replaceRecord(path, { ...pending, state: "accepted", message_id: messageId });
    } catch {
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
    const promise = deliver(validated, key, fingerprint);
    inFlight.set(key, { fingerprint, promise });
    try {
      return await promise;
    } finally {
      inFlight.delete(key);
    }
  };
}
