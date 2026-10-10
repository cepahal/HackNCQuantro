import { createHash, randomUUID } from "node:crypto";
import { lstat, mkdir, open, readdir, readFile, rename, rm } from "node:fs/promises";
import { isIP } from "node:net";
import { join, resolve } from "node:path";
import type { Attachment, Content, Message, Space } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const ATTACHMENT_TIMEOUT_MS = 30_000;
const SEND_TIMEOUT_MS = 12_000;
const MAX_TEXT_CHARS = 16_384;
const SUPPORTED_MEDIA = new Set([
  "image/jpeg", "image/png", "image/webp",
  "video/mp4", "video/quicktime", "video/webm",
]);
const RECEIPT = "Received. Your submission is saved for analysis. The analysis pipeline is not connected yet.";
const INVALID = "Please send one public HTTP/S URL or a supported image/video (JPEG, PNG, WebP, MP4, MOV, WebM; maximum 25 MiB total).";
const RETRY = "I couldn't save your submission. Please try sending it again.";

export type IntakeRecord = {
  schema_version: "photon-intake-1";
  conversation_id: string;
  message_id: string;
  receiving_line: string | null;
  input_type: "url" | "attachment";
  input_url: string | null;
  text: string;
  attachments: { path: string; mime_type: string; name: string; size: number }[];
  status: "received";
  received_at: string;
};

class InvalidInput extends Error {}

function collect(content: Content, texts: string[], attachments: Attachment[], depth = 0): void {
  if (depth > 8 || texts.length + attachments.length > 32) throw new InvalidInput();
  if (content.type === "text") texts.push(content.text);
  else if (content.type === "attachment") attachments.push(content);
  else if (content.type === "reply") collect(content.content, texts, attachments, depth + 1);
  else if (content.type === "group") {
    if (content.items.length > 32) throw new InvalidInput();
    for (const child of content.items) {
      if (child.direction === "inbound") collect(child.content, texts, attachments, depth + 1);
    }
  }
}

function publicHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/\.$/, "").replace(/^\[|\]$/g, "");
  const family = isIP(host);
  if (family === 4) {
    const [a = 0, b = 0, c = 0] = host.split(".").map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224
      || (a === 100 && b >= 64 && b <= 127)
      || (a === 169 && b === 254)
      || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && (b === 0 || b === 168))
      || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100)))
      || (a === 203 && b === 0 && c === 113));
  }
  if (family === 6) {
    // Global-unicast literals only; reject documentation, local, mapped and multicast ranges.
    return /^[23]/.test(host) && !/^2001:(?:db8|0)(?::|$)/.test(host) && !/^3fff:/.test(host);
  }
  return host.includes(".") && !/(^|\.)(localhost|local|internal|intranet|lan|home|test|invalid|onion)$/.test(host)
    && host.split(".").every((label) => /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label));
}

function submissionUrl(text: string): string {
  const matches = text.match(/https?:\/\/[^\s<>"']+/gi) ?? [];
  const first = matches[0];
  if (matches.length !== 1 || !first) throw new InvalidInput();
  let candidate = first.replace(/[.,!?;:]+$/, "");
  // Common prose wrappers should not become part of the URL.
  while (candidate.endsWith(")") && (candidate.match(/\)/g)?.length ?? 0) > (candidate.match(/\(/g)?.length ?? 0)) candidate = candidate.slice(0, -1);
  while (candidate.endsWith("]") && !candidate.includes("[")) candidate = candidate.slice(0, -1);
  let url: URL;
  try { url = new URL(candidate); } catch { throw new InvalidInput(); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !publicHost(url.hostname)) throw new InvalidInput();
  return url.href;
}

async function send(space: Space, text: string): Promise<void> {
  // A transport failure must not discard a successfully saved intake or leak SDK errors.
  try { await withinDeadline(space.send(text), Date.now() + SEND_TIMEOUT_MS); }
  catch { /* A saved request remains available to the pipeline. */ }
}

async function withinDeadline<T>(operation: Promise<T>, deadline: number): Promise<T> {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    void operation.catch(() => undefined);
    throw new Error("Attachment download timed out");
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([operation, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error("Attachment download timed out")), remaining);
    })]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function saveAttachment(attachment: Attachment, path: string, remaining: number, deadline: number): Promise<number> {
  const file = await open(path, "wx", 0o600);
  let reader: ReadableStreamDefaultReader<unknown> | undefined;
  let size = 0;
  let failed = false;
  try {
    const stream = attachment.stream().then((value) => {
      // A provider may resolve after its deadline; release that late stream too.
      if (failed) {
        void value.cancel().catch(() => undefined);
        throw new Error("Attachment download timed out");
      }
      return value;
    });
    reader = (await withinDeadline(stream, deadline)).getReader();
    while (true) {
      const chunk = await withinDeadline(reader.read(), deadline);
      if (chunk.done) break;
      if (!(chunk.value instanceof Uint8Array)) throw new Error("Invalid attachment stream");
      size += chunk.value.byteLength;
      if (size > remaining) throw new Error("Attachment size limit");
      let offset = 0;
      while (offset < chunk.value.byteLength) {
        const written = await file.write(chunk.value, offset, chunk.value.byteLength - offset);
        if (!written.bytesWritten) throw new Error("Attachment write failed");
        offset += written.bytesWritten;
      }
    }
    if (size === 0) throw new Error("Empty attachment");
    await file.sync();
    return size;
  } catch {
    failed = true;
    // Cancellation itself may stall in a broken provider, so it must not delay the retry response.
    if (reader) void reader.cancel().catch(() => undefined);
    throw new Error("Could not save attachment");
  } finally {
    reader?.releaseLock();
    await file.close();
  }
}

/** Persist local intake before acknowledgement. This does not fetch submitted URLs or run analysis. */
export function createReceiver(options: {
  directory: string;
  maxAttachmentBytes?: number;
  onSaved?: (record: IntakeRecord, eventKey: string, space: Space) => void;
}) {
  const directory = resolve(options.directory);
  const maxBytes = options.maxAttachmentBytes ?? MAX_ATTACHMENT_BYTES;
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || maxBytes > MAX_ATTACHMENT_BYTES) throw new Error("Attachment limit must be between 1 byte and 25 MiB.");

  return async function receive(space: Space, message: Message): Promise<void> {
    if (message.direction !== "inbound") return;
    const texts: string[] = [];
    const attachments: Attachment[] = [];
    let text: string;
    let inputUrl: string | null;
    try {
      collect(message.content, texts, attachments);
      text = texts.join("\n").trim();
      if (text.length > MAX_TEXT_CHARS || attachments.length > 32) throw new InvalidInput();
      if (message.platform === "terminal" && attachments.length) throw new InvalidInput();
      let declaredBytes = 0;
      for (const attachment of attachments) {
        if (!SUPPORTED_MEDIA.has(attachment.mimeType.toLowerCase())) throw new InvalidInput();
        if (attachment.size !== undefined) {
          if (!Number.isSafeInteger(attachment.size) || attachment.size < 0) throw new InvalidInput();
          declaredBytes += attachment.size;
        }
      }
      if (declaredBytes > maxBytes) throw new InvalidInput();
      // Attachments are the submitted media; any accompanying text is retained as a caption.
      inputUrl = attachments.length ? null : submissionUrl(text);
    } catch {
      await send(space, INVALID);
      return;
    }

    const line = message.platform === "imessage" ? imessage(space).phone || null : null;
    const eventKey = createHash("sha256").update(JSON.stringify([message.platform, line, space.id, message.id])).digest("hex");
    const eventDirectory = join(directory, eventKey);
    let claimed = false;
    let record: IntakeRecord;
    try {
      await mkdir(directory, { recursive: true, mode: 0o700 });
      try {
        await mkdir(eventDirectory, { mode: 0o700 });
        claimed = true;
      } catch (error) {
        if (error instanceof Error && "code" in error && error.code === "EEXIST") return;
        throw error;
      }
      record = {
        schema_version: "photon-intake-1", conversation_id: space.id, message_id: message.id,
        receiving_line: line, input_type: attachments.length ? "attachment" : "url",
        input_url: inputUrl, text, attachments: [], status: "received", received_at: new Date().toISOString(),
      };
      let total = 0;
      const attachmentDeadline = Date.now() + ATTACHMENT_TIMEOUT_MS;
      for (const attachment of attachments) {
        const path = join(eventDirectory, `${randomUUID()}.bin`);
        const size = await saveAttachment(attachment, path, maxBytes - total, attachmentDeadline);
        total += size;
        record.attachments.push({ path, mime_type: attachment.mimeType.toLowerCase(), name: attachment.name.slice(0, 255), size });
      }
      const temporaryRecord = join(eventDirectory, "request.tmp");
      const file = await open(temporaryRecord, "wx", 0o600);
      try { await file.writeFile(JSON.stringify(record, null, 2) + "\n", "utf8"); await file.sync(); }
      finally { await file.close(); }
      await rename(temporaryRecord, join(eventDirectory, "request.json"));
    } catch {
      if (claimed) await rm(eventDirectory, { recursive: true, force: true }).catch(() => undefined);
      await send(space, RETRY);
      return;
    }
    await send(space, options.onSaved
      ? (record.input_type === "url"
        ? "Received. Your URL is saved and queued for analysis. I’ll send the report here when it is ready."
        : "Your attachment is saved. Automatic analysis currently accepts public post URLs; send a URL to start an investigation.")
      : RECEIPT);
    if (record.input_type === "url") options.onSaved?.(record, eventKey, space);
  };
}

/** Restrict later summary delivery to conversations with a completed local intake record. */
export async function hasConversation(directory: string, conversationId: string): Promise<boolean> {
  let entries;
  try { entries = await readdir(resolve(directory), { withFileTypes: true }); } catch { return false; }
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^[a-f0-9]{64}$/.test(entry.name)) continue;
    const path = join(resolve(directory), entry.name, "request.json");
    try {
      const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink() || info.size > 64 * 1024) continue;
      const value: unknown = JSON.parse(await readFile(path, "utf8"));
      if (typeof value === "object" && value !== null && "schema_version" in value && value.schema_version === "photon-intake-1"
        && "status" in value && value.status === "received" && "conversation_id" in value && value.conversation_id === conversationId) return true;
    } catch { /* Missing, incomplete or malformed records are not delivery authorization. */ }
  }
  return false;
}
