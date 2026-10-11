import type { Message, Space } from "spectrum-ts";

const REPORT_BASE = "https://quantro-ten.vercel.app/results?id=";
const SEND_TIMEOUT_MS = 12_000;
const IGNORED_CONTENT = new Set(["reaction", "typing", "rename", "avatar"]);

export const DEMO_ACK = "Received. Your URL is saved and queued for analysis. I’ll send the report here when it is ready.";

type Demo = { videoId: string; context: string; reportId: string };

const BIDEN: Demo = {
  videoId: "tSj9gkgCM8Q",
  reportId: "report_job_demo_tSj9gkgCM8Q_5",
  context: "Joe Biden says America has \"the strongest economy in the world\" and \"the most productive workers,\" then closes by saying \"Hillary Clinton will write the next chapter\" and \"we own the finish line.\" The source opens with him saying it has \"never been a good bet to bet against America,\" so the available window shows the clip continuing the same optimistic argument.",
};

const DEMOS: readonly Demo[] = [
  {
    videoId: "qzYTN4lfEn4",
    reportId: "report_job_demo_qzYTN4lfEn4_5",
    context: "Bernie Sanders says \"the political system in this country is largely corrupt\" because \"billionaires in both political parties\" decide legislation and candidates, citing Elon Musk and AIPAC primary threats. In the source, he is answering a question about why both parties are viewed so poorly (\"I will tell you why\"), and the interviewer then moves on to Gaza.",
  },
  BIDEN,
  {
    videoId: "3XPuGqqWLtk",
    reportId: "report_job_demo_3XPuGqqWLtk_5",
    context: "Barack Obama tells graduates \"nobody is going to give you anything that you have not earned\" and that \"nobody cares\" about past hardship. In the source, these lines follow his acknowledgment that \"racism and discrimination\" still exist in a \"hypercompetitive world,\" and lead into a tribute to figures such as Frederick Douglass and Dr. Martin Luther King, Jr.",
  },
];

/** Demo routing: a known demo video ID selects its report; anything else gets the Biden demo. */
export function demoReplies(text: string): readonly [string, string, string] {
  const demo = DEMOS.find((candidate) => text.includes(candidate.videoId)) ?? BIDEN;
  return [DEMO_ACK, demo.context, `Full report:\n${REPORT_BASE}${demo.reportId}`];
}

function collectStrings(value: unknown, out: string[], seen: WeakSet<object>, depth = 0): void {
  if (out.length > 64 || depth > 6) return;
  if (typeof value === "string") { out.push(value); return; }
  if (typeof value !== "object" || value === null || seen.has(value)) return;
  seen.add(value);
  for (const child of Array.isArray(value) ? value : Object.values(value)) collectStrings(child, out, seen, depth + 1);
}

export function messageText(message: Message): string {
  const out: string[] = [];
  collectStrings(message.content, out, new WeakSet());
  return out.join("\n");
}

export function wantsDemoReply(message: Message): boolean {
  return message.direction === "inbound" && !IGNORED_CONTENT.has(message.content.type);
}

async function sendQuietly(space: Space, text: string): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([space.send(text), new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error("timeout")), SEND_TIMEOUT_MS);
    })]);
    return true;
  } catch {
    return false;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Replies are sent in order; a failed send is logged without details and never surfaces to the user. */
export function createDemoResponder(options: { summaryDelayMs?: number; linkDelayMs?: number } = {}) {
  const summaryDelayMs = options.summaryDelayMs ?? 4_000;
  const linkDelayMs = options.linkDelayMs ?? 1_200;
  const handled = new Set<string>();
  const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

  return async function respond(space: Space, message: Message): Promise<void> {
    if (!wantsDemoReply(message)) return;
    const key = `${space.id}:${message.id}`;
    if (handled.has(key)) return;
    handled.add(key);
    if (handled.size > 5_000) handled.delete(handled.values().next().value!);
    const [ack, context, link] = demoReplies(messageText(message));
    const results = [await sendQuietly(space, ack)];
    await sleep(summaryDelayMs);
    results.push(await sendQuietly(space, context));
    await sleep(linkDelayMs);
    results.push(await sendQuietly(space, link));
    console.log(`Demo reply sent: ${results.map((ok) => (ok ? "ok" : "failed")).join(", ")}.`);
  };
}
