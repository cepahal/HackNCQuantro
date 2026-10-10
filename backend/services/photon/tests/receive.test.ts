import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import type { AgentSender, Attachment, Content, ContentInput, Message, Space } from "spectrum-ts";
import { createReceiver, hasConversation } from "../src/receive.ts";

async function directory(t: { after(fn: () => Promise<void>): void }): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), "photon-receive-test-"));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}

function space(platform = "terminal", id = "test-conversation", line = "+15555550123") {
  const sent: string[] = [];
  const value = {
    __platform: platform, id, phone: line,
    async send(text: unknown) { assert.equal(typeof text, "string"); sent.push(String(text)); return undefined; },
  } as unknown as Space;
  return { value, sent };
}

type SentMessage = Message<string, AgentSender, Space>;
function fixtureReply(content: ContentInput): Promise<SentMessage | undefined>;
function fixtureReply(...content: [ContentInput, ContentInput, ...ContentInput[]]): Promise<SentMessage[]>;
async function fixtureReply(...content: ContentInput[]): Promise<SentMessage | undefined | SentMessage[]> {
  return content.length > 1 ? [] : undefined;
}

function message(content: Content, chat: Space, options: { id?: string; direction?: "inbound" | "outbound" } = {}): Message {
  return {
    id: options.id ?? "test-message", content, platform: chat.__platform, space: chat,
    direction: options.direction ?? "inbound", timestamp: new Date("2026-10-10T12:00:00Z"), sender: undefined,
    async edit() {}, async react() { return undefined; }, async read() {},
    reply: fixtureReply, async unsend() {},
  };
}

function media(bytes: number[], options: { mime?: string; name?: string; declaredSize?: number; fail?: boolean } = {}): Attachment {
  return {
    type: "attachment", id: "test-attachment", name: options.name ?? "example.mp4",
    mimeType: options.mime ?? "video/mp4", size: options.declaredSize,
    async read() { throw new Error("The receiver must stream attachments."); },
    async stream() {
      if (options.fail) throw new Error("private provider failure must not be sent");
      return new ReadableStream<Uint8Array>({
        start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); },
      });
    },
  };
}

async function records(path: string): Promise<Record<string, unknown>[]> {
  const values: Record<string, unknown>[] = [];
  for (const entry of await readdir(path, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      try { values.push(JSON.parse(await readFile(join(path, entry.name, "request.json"), "utf8"))); }
      catch { /* Incomplete records are not published intake. */ }
    }
  }
  return values;
}

test("public URL intake is durable before acknowledgement and never fetched", async (t) => {
  const path = await directory(t);
  t.mock.method(globalThis, "fetch", async () => { throw new Error("Submitted URLs must not be fetched here."); });
  const chat = space();
  const originalSend = chat.value.send.bind(chat.value);
  t.mock.method(chat.value, "send", async (text: unknown) => {
    assert.equal((await records(path)).length, 1);
    return originalSend(String(text));
  });
  await createReceiver({ directory: path })(chat.value, message({ type: "text", text: "Check (https://example.com/article)." }, chat.value));
  const [record] = await records(path);
  assert.equal(record?.schema_version, "photon-intake-1");
  assert.equal(record?.input_url, "https://example.com/article");
  assert.equal(record?.input_type, "url");
  assert.equal(record?.conversation_id, "test-conversation");
  assert.equal(record?.message_id, "test-message");
  assert.equal(record?.receiving_line, null);
  assert.equal(record?.status, "received");
  assert.ok(Number.isFinite(Date.parse(String(record?.received_at))));
  assert.deepEqual(record?.attachments, []);
  assert.match(chat.sent[0] ?? "", /saved for analysis.*pipeline is not connected/);
});

test("invalid, ambiguous, credential-bearing and local URLs never become intake", async (t) => {
  const path = await directory(t);
  const chat = space();
  const receive = createReceiver({ directory: path });
  for (const text of [
    "hello", "ftp://example.com/file", "https://example.com https://example.org",
    "http://user:password@example.com/", "http://localhost/", "http://localhost.example.local/",
    "http://service.internal/", "http://server/", "http://10.0.0.1/", "http://172.16.0.1/",
    "http://192.168.1.1/", "http://169.254.169.254/", "http://100.64.0.1/",
    "http://127.0.0.1/", "http://2130706433/", "http://0x7f000001/",
    "http://[::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
  ]) await receive(chat.value, message({ type: "text", text }, chat.value, { id: text }));
  assert.deepEqual(await records(path), []);
  assert.ok(chat.sent.every((text) => text.startsWith("Please send one public HTTP/S URL")));
});

test("outbound events are ignored", async (t) => {
  const path = await directory(t);
  const chat = space();
  await createReceiver({ directory: path })(chat.value, message({ type: "text", text: "https://example.com" }, chat.value, { direction: "outbound" }));
  assert.deepEqual(await readdir(path), []);
  assert.deepEqual(chat.sent, []);
});

test("durable and concurrent duplicates save and acknowledge exactly once locally", async (t) => {
  const path = await directory(t);
  const chat = space();
  const incoming = message({ type: "text", text: "https://example.com" }, chat.value);
  const first = createReceiver({ directory: path });
  const second = createReceiver({ directory: path });
  await Promise.all([first(chat.value, incoming), second(chat.value, incoming)]);
  await createReceiver({ directory: path })(chat.value, incoming);
  assert.equal((await records(path)).length, 1);
  assert.equal(chat.sent.length, 1);
});

test("grouped video and image attachments stream to opaque bounded filenames", async (t) => {
  const path = await directory(t);
  const chat = space("imessage");
  const video = media([1, 2, 3], { name: "../../escape.mp4" });
  const image = media([4, 5, 6, 7], { mime: "image/png", name: "picture.png" });
  const incoming = message({ type: "group", items: [
    message({ type: "text", text: "This caption is retained." }, chat.value),
    message(video, chat.value), message(image, chat.value),
    message({ type: "text", text: "Outbound child must be ignored." }, chat.value, { direction: "outbound" }),
  ] }, chat.value);
  await createReceiver({ directory: path, maxAttachmentBytes: 7 })(chat.value, incoming);
  const [record] = await records(path);
  assert.equal(record?.input_type, "attachment");
  assert.equal(record?.input_url, null);
  assert.equal(record?.receiving_line, "+15555550123");
  assert.equal(record?.text, "This caption is retained.");
  const attachments = record?.attachments as { path: string; mime_type: string; size: number; name: string }[];
  assert.equal(attachments.length, 2);
  assert.deepEqual(attachments.map((item) => item.size), [3, 4]);
  assert.equal(attachments[0]?.name, "../../escape.mp4");
  assert.equal(attachments[1]?.mime_type, "image/png");
  for (const item of attachments) {
    assert.equal(dirname(dirname(item.path)), path);
    assert.match(item.path, /[a-f0-9-]{36}\.bin$/);
    assert.equal((await readFile(item.path)).length, item.size);
  }
  assert.equal(chat.sent.length, 1);
});

test("reply processing unwraps new content without submitting the quoted target", async (t) => {
  const path = await directory(t);
  const chat = space();
  const target = message({ type: "text", text: "http://localhost/private" }, chat.value);
  await createReceiver({ directory: path })(chat.value, message({
    type: "reply", content: { type: "text", text: "https://example.org/new" }, target,
  }, chat.value));
  assert.equal((await records(path))[0]?.input_url, "https://example.org/new");
});

test("attachment failures remove partial intake and permit a retry of the same event", async (t) => {
  const path = await directory(t);
  const chat = space("imessage");
  const receive = createReceiver({ directory: path });
  await receive(chat.value, message(media([1], { fail: true }), chat.value));
  assert.deepEqual(await readdir(path), []);
  assert.equal(chat.sent[0], "I couldn't save your submission. Please try sending it again.");
  assert.doesNotMatch(chat.sent[0] ?? "", /private provider failure/);
  await receive(chat.value, message(media([1, 2]), chat.value));
  assert.equal((await records(path)).length, 1);
  assert.match(chat.sent[1] ?? "", /^Received/);
});

test("actual streamed bytes are bounded across attachments despite incorrect size metadata", async (t) => {
  const path = await directory(t);
  const chat = space("imessage");
  await createReceiver({ directory: path, maxAttachmentBytes: 3 })(chat.value, message({ type: "group", items: [
    message(media([1, 2], { declaredSize: 1 }), chat.value),
    message(media([3, 4], { declaredSize: 1 }), chat.value),
  ] }, chat.value));
  assert.deepEqual(await readdir(path), []);
  assert.match(chat.sent[0] ?? "", /couldn't save/);
});

test("stalled attachment setup or reads time out without a saved intake or raw error", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const path = await directory(t);
  const chat = space("imessage");
  for (const stage of ["setup", "read"]) {
    let started: () => void = () => undefined;
    const ready = new Promise<void>((resolve) => { started = resolve; });
    const attachment = media([1]);
    t.mock.method(attachment, "stream", async () => {
      if (stage === "setup") { started(); return new Promise<ReadableStream<Uint8Array>>(() => undefined); }
      return new ReadableStream<Uint8Array>({ pull() { started(); return new Promise<void>(() => undefined); } });
    });
    const pending = createReceiver({ directory: path })(chat.value, message(attachment, chat.value, { id: stage }));
    await ready;
    await new Promise<void>((resolve) => setImmediate(resolve));
    t.mock.timers.tick(30_001);
    await pending;
    assert.deepEqual(await readdir(path), []);
  }
  assert.equal(chat.sent.length, 2);
  assert.ok(chat.sent.every((text) => text === "I couldn't save your submission. Please try sending it again."));
});

test("unsupported media, declared oversize and terminal attachments are rejected without download", async (t) => {
  const path = await directory(t);
  const chat = space("imessage");
  const receive = createReceiver({ directory: path, maxAttachmentBytes: 3 });
  await receive(chat.value, message(media([1], { mime: "application/pdf", fail: true }), chat.value));
  await receive(chat.value, message(media([1], { declaredSize: 4, fail: true }), chat.value));
  const terminal = space();
  await receive(terminal.value, message(media([1], { fail: true }), terminal.value));
  assert.deepEqual(await readdir(path), []);
  assert.ok([...chat.sent, ...terminal.sent].every((text) => text.startsWith("Please send")));
});

test("save failures return safe retry text and successful intake survives acknowledgement failure", async (t) => {
  const path = await directory(t);
  const blocked = join(path, "not-a-directory");
  await writeFile(blocked, "occupied");
  const chat = space();
  await createReceiver({ directory: blocked })(chat.value, message({ type: "text", text: "https://example.com" }, chat.value));
  assert.match(chat.sent[0] ?? "", /couldn't save/);
  const receive = createReceiver({ directory: path });
  t.mock.method(chat.value, "send", async () => { throw new Error("private transport failure"); });
  await receive(chat.value, message({ type: "text", text: "https://example.com" }, chat.value));
  assert.equal((await records(path)).length, 1);
  assert.equal(await hasConversation(path, chat.value.id), true);
});

test("dedup keys distinguish receiving lines and conversations", async (t) => {
  const path = await directory(t);
  const receive = createReceiver({ directory: path });
  for (const chat of [space("imessage", "same-chat", "+15555550123"), space("imessage", "same-chat", "+15555550124"), space("imessage", "other-chat")]) {
    await receive(chat.value, message({ type: "text", text: "https://example.com" }, chat.value));
  }
  assert.equal((await records(path)).length, 3);
});

test("conversation lookup requires a completed record and ignores incomplete, malformed and symlinked directories", async (t) => {
  const path = await directory(t);
  assert.equal(await hasConversation(path, "missing"), false);
  assert.equal(await hasConversation(join(path, "absent"), "missing"), false);
  const incomplete = join(path, "a".repeat(64));
  await mkdir(incomplete);
  await writeFile(join(incomplete, "request.tmp"), JSON.stringify({ schema_version: "photon-intake-1", status: "received", conversation_id: "incomplete" }));
  assert.equal(await hasConversation(path, "incomplete"), false);
  await writeFile(join(incomplete, "request.json"), "not json");
  assert.equal(await hasConversation(path, "incomplete"), false);

  const outside = await directory(t);
  await writeFile(join(outside, "request.json"), JSON.stringify({ schema_version: "photon-intake-1", status: "received", conversation_id: "linked" }));
  await symlink(outside, join(path, "b".repeat(64)), process.platform === "win32" ? "junction" : "dir");
  assert.equal(await hasConversation(path, "linked"), false);

  const chat = space();
  await createReceiver({ directory: path })(chat.value, message({ type: "text", text: "https://example.com" }, chat.value));
  assert.equal(await hasConversation(path, chat.value.id), true);
  assert.equal(await hasConversation(path, "other"), false);
});
