import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { BridgeError, createSummarySender, type SummaryInput } from "../src/summary.ts";

const input: SummaryInput = {
  conversation_id: "imessage-conversation-1",
  summary: "The earlier source was published in 2022.",
  report_url: "https://example.com/reports/1",
};

async function withDirectory(run: (directory: string) => Promise<void>): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "quantro-summary-"));
  try {
    await run(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

function hasError(status: number, code: string) {
  return (error: unknown): boolean => {
    assert.ok(error instanceof BridgeError);
    assert.equal(error.status, status);
    assert.equal(error.body.error.stage, "send_summary");
    assert.equal(error.body.error.code, code);
    assert.equal(error.body.error.retryable, false);
    return true;
  };
}

test("accepted delivery persists before success and is reused after restart", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let sends = 0;
    const options = {
      deliveryDirectory,
      resolveConversation: async (conversationId: string) => {
        assert.equal(conversationId, input.conversation_id);
        const files = await readdir(deliveryDirectory);
        assert.equal(files.length, 1);
        assert.equal(JSON.parse(await readFile(join(deliveryDirectory, files[0]!), "utf8")).state, "pending");
        return {
          send: async (text: string) => {
            sends++;
            assert.equal(text, `${input.summary}\n\nFull report: ${input.report_url}`);
            return { id: "provider-message-1" };
          },
        };
      },
    };
    const expected = { conversation_id: input.conversation_id, message_id: "provider-message-1", status: "accepted" };
    assert.deepEqual(await createSummarySender(options)(input, "job_1"), expected);
    assert.deepEqual(await createSummarySender(options)(input, "job_1"), expected);
    assert.equal(sends, 1);
    const [file] = await readdir(deliveryDirectory);
    assert.match(file!, /^[a-f0-9]{64}\.json$/);
    const ledger = await readFile(join(deliveryDirectory, file!), "utf8");
    assert.equal(JSON.parse(ledger).state, "accepted");
    assert.ok(!ledger.includes(input.summary));
    assert.ok(!ledger.includes(input.report_url));
    assert.ok(!ledger.includes(input.conversation_id));
  });
});

test("concurrent calls on one sender share one provider attempt", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let sends = 0;
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => ({ send: async () => ({ id: `message-${++sends}` }) }),
    });
    const results = await Promise.all(Array.from({ length: 12 }, () => send(input, "job-2")));
    assert.equal(sends, 1);
    assert.ok(results.every((result) => result.message_id === "message-1"));
  });
});

test("exclusive ledger suppresses a second independent sender while delivery is pending", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let release!: () => void;
    let entered!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const started = new Promise<void>((resolve) => { entered = resolve; });
    let sends = 0;
    const options = {
      deliveryDirectory,
      resolveConversation: async () => ({
        send: async () => {
          sends++;
          entered();
          await gate;
          return { id: "once" };
        },
      }),
    };
    const first = createSummarySender(options)(input, "job-3");
    await started;
    try {
      await assert.rejects(createSummarySender(options)(input, "job-3"), hasError(409, "DELIVERY_UNCERTAIN"));
    } finally {
      release();
    }
    await first;
    assert.equal(sends, 1);
  });
});

test("changed payload conflicts while pending and after acceptance", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let entered!: () => void;
    let release!: () => void;
    const started = new Promise<void>((resolve) => { entered = resolve; });
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => ({ send: async () => { entered(); await gate; return { id: "one" }; } }),
    });
    const first = send(input, "job-4");
    await started;
    try {
      await assert.rejects(send({ ...input, summary: "Different" }, "job-4"), hasError(409, "IDEMPOTENCY_CONFLICT"));
    } finally {
      release();
    }
    await first;
    await assert.rejects(send({ ...input, report_url: "https://example.com/other" }, "job-4"), hasError(409, "IDEMPOTENCY_CONFLICT"));
  });
});

test("failed and missing-ID provider attempts never automatically resend", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const kind of ["throw", "missing-id"] as const) {
      let resolutions = 0;
      const options = {
        deliveryDirectory,
        resolveConversation: async () => {
          resolutions++;
          return {
            send: async () => {
              if (kind === "throw") throw new Error("private detail");
              return undefined;
            },
          };
        },
      };
      await assert.rejects(createSummarySender(options)(input, kind), hasError(502, "DELIVERY_UNCERTAIN"));
      await assert.rejects(createSummarySender(options)(input, kind), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.equal(resolutions, 1);
    }
  });
});

test("a conversation preflight failure preserves its error and allows the same job to retry", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let resolutions = 0;
    let sends = 0;
    const unavailable = new BridgeError(409, "CONVERSATION_UNAVAILABLE", "Receive a submission before sending its report.", false);
    const options = {
      deliveryDirectory,
      resolveConversation: async () => {
        resolutions++;
        if (resolutions === 1) throw unavailable;
        return { send: async () => { sends++; return { id: "now-available" }; } };
      },
    };
    await assert.rejects(createSummarySender(options)(input, "preflight-retry"), (error: unknown) => {
      assert.equal(error, unavailable);
      return true;
    });
    assert.equal(sends, 0);
    assert.deepEqual(await readdir(deliveryDirectory), []);
    assert.equal((await createSummarySender(options)(input, "preflight-retry")).message_id, "now-available");
    assert.equal((await createSummarySender(options)(input, "preflight-retry")).message_id, "now-available");
    assert.equal(resolutions, 2);
    assert.equal(sends, 1);
  });
});

test("unexpected resolver failures are normalized and retryable without exposing details", async () => {
  await withDirectory(async (deliveryDirectory) => {
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => { throw new Error("private resolver detail"); },
    });
    await assert.rejects(send(input, "resolver-error"), (error: unknown) => {
      assert.ok(error instanceof BridgeError);
      assert.equal(error.status, 503);
      assert.equal(error.body.error.code, "CONVERSATION_UNAVAILABLE");
      assert.equal(error.body.error.stage, "send_summary");
      assert.equal(error.body.error.retryable, true);
      assert.ok(!JSON.stringify(error.body).includes("private resolver detail"));
      return true;
    });
    assert.deepEqual(await readdir(deliveryDirectory), []);
  });
});

test("failed preflight marker cleanup fails closed without a provider send", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let resolutions = 0;
    const options = {
      deliveryDirectory,
      resolveConversation: async () => {
        resolutions++;
        const [file] = await readdir(deliveryDirectory);
        const marker = join(deliveryDirectory, file!);
        await rm(marker);
        await mkdir(marker);
        throw new Error("resolver unavailable");
      },
    };
    await assert.rejects(createSummarySender(options)(input, "cleanup-failed"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    await assert.rejects(createSummarySender(options)(input, "cleanup-failed"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    assert.equal(resolutions, 1);
  });
});

test("invalid inputs fail before resolving or creating a delivery ledger", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let resolutions = 0;
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => { resolutions++; return { send: async () => ({ id: "one" }) }; },
    });
    for (const invalid of [
      null, {}, { ...input, conversation_id: "" }, { ...input, conversation_id: "x".repeat(513) },
      { ...input, summary: " " }, { ...input, summary: "x".repeat(2001) },
      { ...input, report_url: "file:///private" }, { ...input, report_url: "https://user:secret@example.com" },
      { ...input, report_url: "https://example.com\n/private" },
    ]) {
      await assert.rejects(send(invalid, "valid"), hasError(400, "INVALID_SUMMARY_INPUT"));
    }
    for (const key of ["", "../escape", "x".repeat(129), "has spaces"]) {
      await assert.rejects(send(input, key), hasError(400, "INVALID_IDEMPOTENCY_KEY"));
    }
    await assert.rejects(Reflect.apply(send, undefined, [input, undefined]), hasError(400, "INVALID_IDEMPOTENCY_KEY"));
    assert.equal(resolutions, 0);
    assert.deepEqual(await readdir(deliveryDirectory), []);
  });
});

test("development localhost report links are allowed without fetching them", async () => {
  await withDirectory(async (deliveryDirectory) => {
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => ({ send: async () => ({ id: "local-report" }) }),
    });
    assert.equal((await send({ ...input, report_url: "http://localhost:3000/report/1" }, "local")).status, "accepted");
  });
});

test("storage failure and corrupt existing ledgers fail closed", async () => {
  await withDirectory(async (directory) => {
    let resolutions = 0;
    const resolveConversation = async () => { resolutions++; return { send: async () => ({ id: "unexpected" }) }; };
    const file = join(directory, "regular-file");
    await writeFile(file, "not a directory");
    await assert.rejects(createSummarySender({ deliveryDirectory: file, resolveConversation })(input, "storage"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    const key = createHash("sha256").update(JSON.stringify([input.conversation_id, "corrupt"])).digest("hex");
    await writeFile(join(directory, `${key}.json`), "partial JSON");
    await assert.rejects(createSummarySender({ deliveryDirectory: directory, resolveConversation })(input, "corrupt"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    assert.equal(resolutions, 0);
  });
});

test("the same job can deliver separately to distinct conversations", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let sends = 0;
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => ({ send: async () => ({ id: `message-${++sends}` }) }),
    });
    await send(input, "shared-job");
    await send({ ...input, conversation_id: "other-conversation" }, "shared-job");
    assert.equal(sends, 2);
  });
});

test("a missing persisted acknowledgement after provider acceptance never triggers another send", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let sends = 0;
    const options = {
      deliveryDirectory,
      resolveConversation: async () => ({
        send: async () => {
          sends++;
          const [file] = await readdir(deliveryDirectory);
          const marker = join(deliveryDirectory, file!);
          await rm(marker);
          await mkdir(marker);
          return { id: "accepted-but-unrecorded" };
        },
      }),
    };
    await assert.rejects(createSummarySender(options)(input, "storage-after-send"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    await assert.rejects(createSummarySender(options)(input, "storage-after-send"), hasError(503, "DELIVERY_STORAGE_UNAVAILABLE"));
    assert.equal(sends, 1);
  });
});

test("invalid provider message IDs leave an ambiguous attempt that cannot resend", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const [index, id] of ["", "x".repeat(513), "id\nprivate"].entries()) {
      let sends = 0;
      const options = {
        deliveryDirectory,
        resolveConversation: async () => ({ send: async () => { sends++; return { id }; } }),
      };
      await assert.rejects(createSummarySender(options)(input, `bad-id-${index}`), hasError(502, "DELIVERY_UNCERTAIN"));
      await assert.rejects(createSummarySender(options)(input, `bad-id-${index}`), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.equal(sends, 1);
    }
  });
});

test("stalled provider sends time out and late success or rejection cannot cause a resend", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const outcome of ["success", "rejection"] as const) {
      let complete!: (message: { id: string }) => void;
      let fail!: (error: Error) => void;
      const providerResult = new Promise<{ id: string }>((resolve, reject) => {
        complete = resolve;
        fail = reject;
      });
      let sends = 0;
      const options = {
        deliveryDirectory,
        sendTimeoutMs: 10,
        resolveConversation: async () => ({ send: () => { sends++; return providerResult; } }),
      };
      await assert.rejects(createSummarySender(options)(input, `timeout-${outcome}`), hasError(502, "DELIVERY_UNCERTAIN"));
      if (outcome === "success") complete({ id: "too-late" });
      else fail(new Error("private late failure"));
      await new Promise<void>((resolve) => setImmediate(resolve));
      await assert.rejects(createSummarySender(options)(input, `timeout-${outcome}`), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.equal(sends, 1);
      const key = createHash("sha256").update(JSON.stringify([input.conversation_id, `timeout-${outcome}`])).digest("hex");
      assert.equal(JSON.parse(await readFile(join(deliveryDirectory, `${key}.json`), "utf8")).state, "failed");
    }
  });
});

test("provider send deadline configuration rejects values outside 1–12000 integer milliseconds", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const sendTimeoutMs of [0, -1, 12_001, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      assert.throws(() => createSummarySender({
        deliveryDirectory,
        sendTimeoutMs,
        resolveConversation: async () => ({ send: async () => ({ id: "unused" }) }),
      }), hasError(500, "INVALID_SEND_TIMEOUT"));
    }
    assert.deepEqual(await readdir(deliveryDirectory), []);
  });
});
