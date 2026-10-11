import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { resolve } from "node:path";

const E164 = /^\+[1-9]\d{6,14}$/;
const PROJECT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_RESPONSE_BYTES = 32_768;
export const WELCOME_MESSAGE = "Quantro is ready. Reply with a public video link to analyze its source and context.";

export interface EnrollmentResult {
  assigned_phone_number: string;
  open_messages_url: string;
  welcome_status: "accepted" | "uncertain";
  already_enrolled: boolean;
}

export interface WelcomeDiagnostic {
  category: "sdk_exception" | "send_deadline" | "invalid_message_id" | "receipt_storage_failed";
  elapsed_ms: number;
  grpcCode?: number;
}

class WelcomeDeadlineError extends Error {}
class WelcomeMessageIdError extends Error {}

export class EnrollmentError extends Error {
  readonly status: number;
  readonly body: { error: { code: string; stage: "enroll"; message: string; retryable: boolean } };

  constructor(status: number, code: string, message: string, retryable = false) {
    super(message);
    this.name = "EnrollmentError";
    this.status = status;
    this.body = { error: { code, stage: "enroll", message, retryable } };
  }
}

interface EnrollmentOptions {
  projectId: string;
  projectSecret: string;
  deliveryDirectory: string;
  sendWelcome: (phone: string, text: string) => Promise<{ id: string } | undefined>;
  fetch?: typeof globalThis.fetch;
  requestTimeoutMs?: number;
  sendTimeoutMs?: number;
  diagnostic?: (event: WelcomeDiagnostic) => void;
}

interface WelcomeRecord {
  version: 1;
  state: "pending" | "accepted" | "uncertain";
  assigned_phone_number: string;
  message_id?: string;
}

const digest = (phone: string): string => createHash("sha256").update(phone).digest("hex");
const unavailable = (): EnrollmentError => new EnrollmentError(
  503, "ENROLLMENT_UNAVAILABLE", "Photon enrollment is unavailable. Try again later.", true,
);
const storageError = (): EnrollmentError => new EnrollmentError(
  503, "ENROLLMENT_STORAGE_UNAVAILABLE", "Welcome delivery state could not be safely stored. No automatic resend is permitted.",
);

function validateInput(input: unknown, key: string): string {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    throw new EnrollmentError(400, "INVALID_ENROLLMENT_INPUT", "Provide an international phone number and explicit messaging consent.");
  }
  const record = input as Record<string, unknown>;
  const phone = record.phone_number;
  if (typeof phone !== "string" || !E164.test(phone) || record.consent !== true
      || Object.keys(record).some((field) => field !== "phone_number" && field !== "consent")) {
    throw new EnrollmentError(400, "INVALID_ENROLLMENT_INPUT", "Provide an international phone number and explicit messaging consent.");
  }
  if (key !== `enroll_${digest(phone)}`) {
    throw new EnrollmentError(400, "INVALID_IDEMPOTENCY_KEY", "The enrollment idempotency key must match the submitted phone number.");
  }
  return phone;
}

function validMessageId(id: unknown): id is string {
  return typeof id === "string" && id.trim().length > 0 && id.length <= 512 && !/[\u0000-\u001f\u007f]/.test(id);
}

function parseRecord(raw: string): WelcomeRecord {
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw storageError();
  const record = value as Record<string, unknown>;
  if (record.version !== 1 || !["pending", "accepted", "uncertain"].includes(String(record.state))
      || typeof record.assigned_phone_number !== "string" || !E164.test(record.assigned_phone_number)
      || (record.state === "accepted" && !validMessageId(record.message_id))) throw storageError();
  return { version: 1, state: record.state as WelcomeRecord["state"],
    assigned_phone_number: record.assigned_phone_number,
    ...(validMessageId(record.message_id) ? { message_id: record.message_id } : {}) };
}

async function existingRecord(path: string): Promise<WelcomeRecord | undefined> {
  try {
    const raw = await readFile(path, "utf8");
    if (raw.length > 4_096) throw storageError();
    return parseRecord(raw);
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") return undefined;
    throw storageError();
  }
}

async function writeRecord(path: string, record: WelcomeRecord): Promise<void> {
  const file = await open(path, "wx", 0o600);
  try {
    await file.writeFile(JSON.stringify(record), "utf8");
    await file.sync();
  } finally {
    await file.close();
  }
}

async function replaceRecord(path: string, record: WelcomeRecord): Promise<void> {
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeRecord(temporary, record);
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => undefined);
  }
}

function result(record: WelcomeRecord, alreadyEnrolled: boolean): EnrollmentResult {
  return { assigned_phone_number: record.assigned_phone_number,
    open_messages_url: `sms:${record.assigned_phone_number}&body=${encodeURIComponent("Hi Quantro")}`,
    welcome_status: record.state === "accepted" ? "accepted" : "uncertain", already_enrolled: alreadyEnrolled };
}

async function boundedJson(response: Response): Promise<unknown> {
  if (!response.headers.get("content-type")?.toLowerCase().includes("application/json") || !response.body) throw unavailable();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_RESPONSE_BYTES) throw unavailable();
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown;
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function providerUser(value: unknown, phone: string): { id: string; assignedPhoneNumber: string } {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw unavailable();
  const user = value as Record<string, unknown>;
  if (user.type !== "shared" || user.phoneNumber !== phone
      || typeof user.id !== "string" || !PROJECT_ID.test(user.id)
      || typeof user.assignedPhoneNumber !== "string" || !E164.test(user.assignedPhoneNumber)) throw unavailable();
  return { id: user.id.toLowerCase(), assignedPhoneNumber: user.assignedPhoneNumber };
}

async function withDeadline<T>(operation: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([operation, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new WelcomeDeadlineError()), timeoutMs);
    })]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export function createEnrollmentHandler(options: EnrollmentOptions) {
  if (!PROJECT_ID.test(options.projectId) || !options.projectSecret.trim()
      || /[\u0000-\u001f\u007f]/.test(options.projectSecret)) throw unavailable();
  const requestTimeoutMs = options.requestTimeoutMs ?? 6_000;
  const sendTimeoutMs = options.sendTimeoutMs ?? 12_000;
  if (![requestTimeoutMs, sendTimeoutMs].every((value) => Number.isInteger(value) && value > 0 && value <= 12_000)) throw unavailable();
  const directory = resolve(options.deliveryDirectory);
  const fetcher = options.fetch ?? globalThis.fetch;
  const endpoint = `https://spectrum.photon.codes/projects/${options.projectId}/users/`;
  const authorization = `Basic ${Buffer.from(`${options.projectId}:${options.projectSecret}`, "utf8").toString("base64")}`;
  const inFlight = new Map<string, Promise<EnrollmentResult>>();

  function diagnose(category: WelcomeDiagnostic["category"], started: number, error?: unknown): void {
    // Never log provider text, stack, identifiers, or arbitrary enumerable fields.
    const event: WelcomeDiagnostic = { category, elapsed_ms: Math.max(0, Math.round(performance.now() - started)) };
    try {
      if (category === "sdk_exception" && typeof error === "object" && error !== null) {
        for (const field of ["grpcCode", "code"]) {
          const code = Object.getOwnPropertyDescriptor(error, field)?.value;
          if (typeof code === "number" && Number.isInteger(code) && code >= 0 && code <= 16) {
            event.grpcCode = code;
            break;
          }
        }
      }
    } catch { /* Untrusted getters/proxies cannot disrupt enrollment or diagnostics. */ }
    try {
      if (options.diagnostic) {
        void Promise.resolve(options.diagnostic(event)).catch(() => undefined);
      } else {
        console.warn("Photon welcome diagnostic:", JSON.stringify(event));
      }
    } catch { /* Diagnostics must never change delivery state or the signup response. */ }
  }

  async function request(url: string, method: "GET" | "POST", phone: string): Promise<Record<string, unknown>> {
    try {
      const response = await fetcher(url, { method, redirect: "error", cache: "no-store",
        signal: AbortSignal.timeout(requestTimeoutMs), headers: { authorization, "Content-Type": "application/json" },
        ...(method === "POST" ? { body: JSON.stringify({ type: "shared", phoneNumber: phone }) } : {}) });
      if (response.status === 409) {
        await response.body?.cancel();
        throw new EnrollmentError(409, "ENROLLMENT_CAPACITY_OR_CONFLICT", "Photon cannot add this user on the current plan. No upgrade was attempted.");
      }
      if (!response.ok) {
        await response.body?.cancel();
        throw unavailable();
      }
      const payload = await boundedJson(response);
      if (typeof payload !== "object" || payload === null || Array.isArray(payload)) throw unavailable();
      const envelope = payload as Record<string, unknown>;
      if (envelope.succeed !== true || typeof envelope.data !== "object" || envelope.data === null || Array.isArray(envelope.data)) throw unavailable();
      return envelope.data as Record<string, unknown>;
    } catch (error) {
      if (error instanceof EnrollmentError) throw error;
      throw unavailable();
    }
  }

  async function enroll(phone: string): Promise<EnrollmentResult> {
    // Keep legacy receipts intact, but never treat a receipt as current authorization.
    const legacyPath = resolve(directory, `${digest(`${options.projectId}:${phone}`)}.json`);
    await existingRecord(legacyPath);
    const listed = await request(`${endpoint}?type=shared&search=${encodeURIComponent(phone)}&limit=100`, "GET", phone);
    if (!Array.isArray(listed.users) || listed.users.length > 100) throw unavailable();
    const matching = listed.users.filter((value: unknown) => typeof value === "object" && value !== null
      && !Array.isArray(value) && (value as Record<string, unknown>).phoneNumber === phone);
    if (matching.length > 1) throw unavailable();
    const alreadyEnrolled = matching.length === 1;
    const user = providerUser(alreadyEnrolled ? matching[0] : await request(endpoint, "POST", phone), phone);
    // Photon may restore an earlier user ID after deletion. Keep its welcome
    // history, but use this lookup to describe the current registration.
    const path = resolve(directory, `${digest(`${options.projectId}:${phone}:${user.id}`)}.json`);
    const reconciledResult = (record: WelcomeRecord): EnrollmentResult => result({
      ...record, assigned_phone_number: user.assignedPhoneNumber,
      state: alreadyEnrolled && record.assigned_phone_number === user.assignedPhoneNumber
        ? record.state : "uncertain",
    }, alreadyEnrolled);
    // A removed-and-restored user can retain the same provider ID. Keep its
    // historical receipt, but never revive that welcome on the next signup.
    const reconciliationPath = `${path}.reconciled.json`;
    const historical = await existingRecord(path);
    const reconciled = await existingRecord(reconciliationPath);
    const existing = reconciled ?? historical;
    if (existing) {
      if (!alreadyEnrolled || existing.assigned_phone_number !== user.assignedPhoneNumber) {
        const uncertain: WelcomeRecord = { version: 1, state: "uncertain",
          assigned_phone_number: user.assignedPhoneNumber };
        try {
          await replaceRecord(reconciliationPath, uncertain);
        } catch { throw storageError(); }
        return result(uncertain, alreadyEnrolled);
      }
      return reconciledResult(existing);
    }
    const pending: WelcomeRecord = { version: 1, state: alreadyEnrolled ? "uncertain" : "pending",
      assigned_phone_number: user.assignedPhoneNumber };
    try {
      await mkdir(directory, { recursive: true, mode: 0o700 });
      // A synced exclusive marker must precede every possible SDK send.
      await writeRecord(path, pending);
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "EEXIST") {
        const concurrent = await existingRecord(path);
        if (concurrent) return reconciledResult(concurrent);
      }
      throw storageError();
    }
    // A provider user without local history may already have received a welcome
    // before a redeploy. Do not turn lost receipt history into another message.
    if (alreadyEnrolled) return result(pending, true);
    let sent: { id: string } | undefined;
    const started = performance.now();
    try {
      // A timeout cannot cancel delivery. Keep pending/uncertain forever rather than resend.
      sent = await withDeadline(options.sendWelcome(phone, WELCOME_MESSAGE), sendTimeoutMs);
      if (!validMessageId(sent?.id)) throw new WelcomeMessageIdError();
    } catch (error) {
      diagnose(error instanceof WelcomeDeadlineError ? "send_deadline"
        : error instanceof WelcomeMessageIdError ? "invalid_message_id" : "sdk_exception", started, error);
      await replaceRecord(path, { ...pending, state: "uncertain" }).catch(() => undefined);
      return result(pending, alreadyEnrolled);
    }
    const accepted: WelcomeRecord = { ...pending, state: "accepted", message_id: sent.id };
    try {
      await replaceRecord(path, accepted);
    } catch {
      // Sending happened; the original pending marker still prevents another attempt.
      diagnose("receipt_storage_failed", started);
      return result(pending, alreadyEnrolled);
    }
    return result(accepted, alreadyEnrolled);
  }

  return async function handleEnrollment(input: unknown, key: string): Promise<EnrollmentResult> {
    const phone = validateInput(input, key);
    const active = inFlight.get(phone);
    if (active) return active;
    const promise = enroll(phone);
    inFlight.set(phone, promise);
    try {
      return await promise;
    } finally {
      inFlight.delete(phone);
    }
  };
}
