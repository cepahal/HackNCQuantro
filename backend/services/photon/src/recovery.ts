import { createHash } from "node:crypto";
import { lstat, open, opendir } from "node:fs/promises";
import type { IntakeRecord } from "./receive.ts";

export const RECOVERY_LIMITS = { entries: 256, jobs: 32, milliseconds: 60_000 } as const;

/** Read regular private records without following known symlinks or allocating unbounded files. */
export async function readPrivateJson(path: string, maximum: number): Promise<unknown> {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size < 1 || info.size > maximum) throw new Error("Invalid private record");
  const file = await open(path, "r");
  try {
    const current = await file.stat();
    if (!current.isFile() || current.size > maximum) throw new Error("Invalid private record");
    const buffer = Buffer.alloc(maximum + 1);
    let length = 0;
    while (length < buffer.length) {
      const result = await file.read(buffer, length, buffer.length - length, length);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    if (length > maximum) throw new Error("Invalid private record");
    return JSON.parse(buffer.toString("utf8", 0, length)) as unknown;
  } finally { await file.close(); }
}

export async function privatePathExists(path: string): Promise<boolean> {
  try { await lstat(path); return true; }
  catch (error) { return !(error instanceof Error && "code" in error && error.code === "ENOENT"); }
}

export async function* recoveryEntries(path: string): AsyncGenerator<{ name: string; directory: boolean; file: boolean }> {
  let directory;
  try {
    const info = await lstat(path);
    if (!info.isDirectory() || info.isSymbolicLink()) return;
    directory = await opendir(path, { bufferSize: 32 });
  } catch { return; }
  for await (const entry of directory) yield { name: entry.name, directory: entry.isDirectory(), file: entry.isFile() };
}

/** Recovery authorizes only completed native iMessage URL intake, never terminal fixtures. */
export function savedUrlIntake(value: unknown, eventKey: string): IntakeRecord | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (record.schema_version !== "photon-intake-1" || record.status !== "received" || record.input_type !== "url"
      || typeof record.conversation_id !== "string" || !record.conversation_id || record.conversation_id.length > 256
      || typeof record.message_id !== "string" || !record.message_id || record.message_id.length > 512
      || !(record.receiving_line === null || (typeof record.receiving_line === "string" && record.receiving_line.length <= 128))
      || typeof record.input_url !== "string" || record.input_url.length > 8192
      || typeof record.text !== "string" || record.text.length > 16_384
      || !Array.isArray(record.attachments) || record.attachments.length !== 0
      || typeof record.received_at !== "string" || !record.received_at.endsWith("Z") || !Number.isFinite(Date.parse(record.received_at))) return undefined;
  try {
    const url = new URL(record.input_url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return undefined;
  } catch { return undefined; }
  const expected = createHash("sha256").update(JSON.stringify([
    "imessage", record.receiving_line, record.conversation_id, record.message_id,
  ])).digest("hex");
  return eventKey === expected ? record as IntakeRecord : undefined;
}

export async function withinRecoveryDeadline<T>(operation: () => Promise<T>, deadline: number, maximum = 5_000): Promise<T> {
  const timeout = Math.min(maximum, deadline - Date.now());
  if (timeout <= 0) throw new Error("Recovery budget reached");
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([operation(), new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error("Recovery lookup timed out")), timeout);
    })]);
  } finally { if (timer) clearTimeout(timer); }
}
