import assert from "node:assert/strict";
import test from "node:test";
import type { Content, Message, Space } from "spectrum-ts";
import { createDemoResponder, DEMO_ACK, demoReplies } from "../src/demo.ts";
import { readConfig } from "../src/config.ts";

function space(options: { fail?: boolean } = {}) {
  const sent: string[] = [];
  const value = {
    __platform: "imessage", id: "demo-conversation",
    async send(text: unknown) {
      if (options.fail) throw new Error("provider down");
      sent.push(String(text));
      return undefined;
    },
  } as unknown as Space;
  return { value, sent };
}

function message(content: Content, chat: Space, id = "m1", direction: "inbound" | "outbound" = "inbound"): Message {
  return { id, content, platform: "imessage", space: chat, direction, timestamp: new Date(), sender: undefined } as unknown as Message;
}

const BIDEN_LINK = "Full report:\nhttps://quantro-ten.vercel.app/results?id=report_job_demo_tSj9gkgCM8Q_5";

test("known demo video IDs route to their own report", () => {
  assert.equal(demoReplies("https://youtu.be/qzYTN4lfEn4")[2], "Full report:\nhttps://quantro-ten.vercel.app/results?id=report_job_demo_qzYTN4lfEn4_5");
  assert.match(demoReplies("https://youtu.be/qzYTN4lfEn4")[1], /^Bernie Sanders/);
  assert.equal(demoReplies("https://www.youtube.com/watch?v=3XPuGqqWLtk")[2], "Full report:\nhttps://quantro-ten.vercel.app/results?id=report_job_demo_3XPuGqqWLtk_5");
  assert.equal(demoReplies("https://www.youtube.com/watch?v=tSj9gkgCM8Q")[2], BIDEN_LINK);
});

test("any other text falls back to the Biden demo", () => {
  for (const text of ["hi", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", ""]) {
    const [ack, context, link] = demoReplies(text);
    assert.equal(ack, DEMO_ACK);
    assert.match(context, /^Joe Biden says America has/);
    assert.equal(link, BIDEN_LINK);
  }
});

test("responder sends ack, context, then link once per message, including rich links", async () => {
  const chat = space();
  const respond = createDemoResponder({ summaryDelayMs: 0, linkDelayMs: 0 });
  const inbound = message({ type: "richlink", url: "https://youtu.be/3XPuGqqWLtk" } as Content, chat.value);
  await respond(chat.value, inbound);
  await respond(chat.value, inbound);
  assert.equal(chat.sent.length, 3);
  assert.equal(chat.sent[0], DEMO_ACK);
  assert.match(chat.sent[1]!, /^Barack Obama/);
  assert.match(chat.sent[2]!, /report_job_demo_3XPuGqqWLtk_5$/);
});

test("reactions, typing and outbound messages get no reply", async () => {
  const chat = space();
  const respond = createDemoResponder({ summaryDelayMs: 0, linkDelayMs: 0 });
  await respond(chat.value, message({ type: "reaction" } as unknown as Content, chat.value, "r"));
  await respond(chat.value, message({ type: "typing" } as unknown as Content, chat.value, "t"));
  await respond(chat.value, message({ type: "text", text: "hi" } as Content, chat.value, "o", "outbound"));
  assert.deepEqual(chat.sent, []);
});

test("send failures never throw or produce an error reply", async () => {
  const chat = space({ fail: true });
  const respond = createDemoResponder({ summaryDelayMs: 0, linkDelayMs: 0 });
  await respond(chat.value, message({ type: "text", text: "hello" } as Content, chat.value));
  assert.deepEqual(chat.sent, []);
});

test("demo replies default on and can be disabled", () => {
  const env = { PHOTON_BRIDGE_TOKEN: "a".repeat(32) };
  assert.equal(readConfig(env, []).demoReplies, true);
  assert.equal(readConfig({ ...env, PHOTON_DEMO_REPLIES: "false" }, []).demoReplies, false);
});
