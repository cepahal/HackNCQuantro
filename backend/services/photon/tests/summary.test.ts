import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { BridgeError, createSummarySender, formatSummaryMessages, type SummaryDiagnostic, type SummaryInput } from "../src/summary.ts";

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
            assert.equal(text, formatSummaryMessages(input)[sends - 1]);
            return { id: "provider-message-1" };
          },
        };
      },
    };
    const expected = { conversation_id: input.conversation_id, message_id: "provider-message-1", status: "accepted" };
    assert.deepEqual(await createSummarySender(options)(input, "job_1"), expected);
    assert.deepEqual(await createSummarySender(options)(input, "job_1"), expected);
    assert.equal(sends, 3);
    const [file] = await readdir(deliveryDirectory);
    assert.match(file!, /^[a-f0-9]{64}\.json$/);
    const ledger = await readFile(join(deliveryDirectory, file!), "utf8");
    assert.equal(JSON.parse(ledger).state, "accepted");
    assert.equal(JSON.parse(ledger).message_ids.length, 3);
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
    assert.equal(sends, 3);
    assert.ok(results.every((result) => result.message_id === "message-3"));
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
    assert.equal(sends, 3);
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
    assert.equal(sends, 3);
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

test("report preparation runs after preflight, fails without sending, and safely retries", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let resolutions = 0;
    let preparations = 0;
    let sends = 0;
    const options = {
      deliveryDirectory,
      resolveConversation: async () => {
        resolutions++;
        return { send: async () => { sends++; return { id: "prepared-message" }; } };
      },
      prepareReport: async (value: SummaryInput, jobId: string) => {
        preparations++;
        assert.deepEqual(value, input);
        assert.equal(jobId, "prepare-job");
        assert.equal(resolutions, preparations);
        assert.equal(sends, 0);
        if (preparations === 1) throw new Error("private preparation details");
      },
    };
    await assert.rejects(createSummarySender(options)(input, "prepare-job"), (error: unknown) => {
      assert.ok(error instanceof BridgeError);
      assert.equal(error.body.error.code, "DELIVERY_STORAGE_UNAVAILABLE");
      assert.equal(error.status, 503);
      assert.ok(!JSON.stringify(error.body).includes("private preparation details"));
      return true;
    });
    assert.equal(sends, 0);
    assert.deepEqual(await readdir(deliveryDirectory), []);
    assert.equal((await createSummarySender(options)(input, "prepare-job")).message_id, "prepared-message");
    assert.equal((await createSummarySender(options)(input, "prepare-job")).message_id, "prepared-message");
    assert.equal(preparations, 2);
    assert.equal(sends, 3);
  });
});

test("invalid inputs and failed conversation preflight never prepare a public report", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let preparations = 0;
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => { throw new BridgeError(409, "CONVERSATION_UNAVAILABLE", "Missing conversation."); },
      prepareReport: async () => { preparations++; },
    });
    await assert.rejects(send(null, "prepare-invalid"), hasError(400, "INVALID_SUMMARY_INPUT"));
    await assert.rejects(send(input, "prepare-preflight"), hasError(409, "CONVERSATION_UNAVAILABLE"));
    assert.equal(preparations, 0);
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
      { ...input, summary: "invalid\u0000text" },
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
    assert.equal(sends, 6);
  });
});

test("a missing persisted acknowledgement after provider acceptance never triggers another send", async () => {
  await withDirectory(async (deliveryDirectory) => {
    let sends = 0;
    const diagnostics: SummaryDiagnostic[] = [];
    const options = {
      deliveryDirectory,
      diagnostic: (event: SummaryDiagnostic) => { diagnostics.push(event); },
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
    assert.equal(diagnostics.length, 1);
    assertSafeDiagnostic(diagnostics[0]!, "receipt_storage_failed");
  });
});

test("invalid provider message IDs leave an ambiguous attempt that cannot resend", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const [index, id] of ["", "x".repeat(513), "id\nprivate"].entries()) {
      let sends = 0;
      const diagnostics: SummaryDiagnostic[] = [];
      const options = {
        deliveryDirectory,
        diagnostic: (event: SummaryDiagnostic) => { diagnostics.push(event); },
        resolveConversation: async () => ({ send: async () => { sends++; return { id }; } }),
      };
      await assert.rejects(createSummarySender(options)(input, `bad-id-${index}`), hasError(502, "DELIVERY_UNCERTAIN"));
      await assert.rejects(createSummarySender(options)(input, `bad-id-${index}`), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.equal(sends, 1);
      assert.equal(diagnostics.length, 1);
      assertSafeDiagnostic(diagnostics[0]!, "invalid_message_id");
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
      const diagnostics: SummaryDiagnostic[] = [];
      const options = {
        deliveryDirectory,
        sendTimeoutMs: 10,
        diagnostic: (event: SummaryDiagnostic) => { diagnostics.push(event); },
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
      assert.equal(diagnostics.length, 1);
      assertSafeDiagnostic(diagnostics[0]!, "send_deadline");
    }
  });
});

function assertSafeDiagnostic(event: SummaryDiagnostic, category: SummaryDiagnostic["category"], grpcCode?: number): void {
  assert.equal(event.category, category);
  assert.ok(Number.isInteger(event.elapsed_ms) && event.elapsed_ms >= 0);
  assert.deepEqual(Object.keys(event).sort(), grpcCode === undefined
    ? ["category", "elapsed_ms"] : ["category", "elapsed_ms", "grpcCode"]);
  assert.equal(event.grpcCode, grpcCode);
}

test("SDK diagnostics expose only a fixed category, elapsed time, and allowlisted numeric gRPC code", async () => {
  await withDirectory(async (deliveryDirectory) => {
    const diagnostics: SummaryDiagnostic[] = [];
    let sends = 0;
    const privateDetail = "private-token-phone-message-content";
    const options = {
      deliveryDirectory,
      diagnostic: (event: SummaryDiagnostic) => { diagnostics.push(event); },
      resolveConversation: async () => ({ send: async () => {
        sends++;
        throw Object.assign(new Error(privateDetail), {
          code: 7, context: { secret: privateDetail, conversation_id: input.conversation_id },
          report_url: input.report_url, stack: privateDetail,
        });
      } }),
    };
    await assert.rejects(createSummarySender(options)(input, "sdk-diagnostic"), (error: unknown) => {
      assert.ok(hasError(502, "DELIVERY_UNCERTAIN")(error));
      assert.ok(!JSON.stringify((error as BridgeError).body).includes(privateDetail));
      return true;
    });
    await assert.rejects(createSummarySender(options)(input, "sdk-diagnostic"), hasError(409, "DELIVERY_UNCERTAIN"));
    assert.equal(sends, 1);
    assert.equal(diagnostics.length, 1);
    assertSafeDiagnostic(diagnostics[0]!, "sdk_exception", 7);
    const log = JSON.stringify(diagnostics);
    for (const forbidden of [privateDetail, input.conversation_id, input.summary, input.report_url]) {
      assert.ok(!log.includes(forbidden));
    }
  });
});

test("diagnostics reject arbitrary SDK codes and never invoke error getters", async () => {
  await withDirectory(async (deliveryDirectory) => {
    const errors: unknown[] = [
      ...[-1, 17, 1.5, Number.NaN, Number.POSITIVE_INFINITY, "7", "PERMISSION_DENIED"].map((code) => ({ code })),
      new Proxy({}, { getOwnPropertyDescriptor: () => { throw new Error("private proxy detail"); } }),
    ];
    let getterReads = 0;
    errors.push(Object.defineProperty({}, "code", { get: () => { getterReads++; throw new Error("private getter detail"); } }));
    for (const [index, error] of errors.entries()) {
      const diagnostics: SummaryDiagnostic[] = [];
      const send = createSummarySender({
        deliveryDirectory,
        diagnostic: (event) => { diagnostics.push(event); },
        resolveConversation: async () => ({ send: async () => { throw error; } }),
      });
      await assert.rejects(send(input, `unsafe-code-${index}`), hasError(502, "DELIVERY_UNCERTAIN"));
      assert.equal(diagnostics.length, 1);
      assertSafeDiagnostic(diagnostics[0]!, "sdk_exception");
    }
    assert.equal(getterReads, 0);
  });
});

test("native Advanced iMessage diagnostics prefer numeric grpcCode without exposing string SDK codes", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const [index, error] of [
      { code: "MESSAGE_SEND_FAILED", grpcCode: 7 },
      { code: 14, grpcCode: 0 },
      { code: 14, grpcCode: "private-native-status" },
    ].entries()) {
      const diagnostics: SummaryDiagnostic[] = [];
      const send = createSummarySender({
        deliveryDirectory,
        diagnostic: (event) => { diagnostics.push(event); },
        resolveConversation: async () => ({ send: async () => { throw error; } }),
      });
      await assert.rejects(send(input, `native-grpc-${index}`), hasError(502, "DELIVERY_UNCERTAIN"));
      assert.equal(diagnostics.length, 1);
      assertSafeDiagnostic(diagnostics[0]!, "sdk_exception", [7, 0, 14][index]);
      assert.ok(!JSON.stringify(diagnostics).includes("MESSAGE_SEND_FAILED"));
      assert.ok(!JSON.stringify(diagnostics).includes("private-native-status"));
    }
    let getterReads = 0;
    const nativeError = Object.defineProperty({ code: 14 }, "grpcCode", {
      get: () => { getterReads++; throw new Error("private getter detail"); },
    });
    const diagnostics: SummaryDiagnostic[] = [];
    await assert.rejects(createSummarySender({
      deliveryDirectory,
      diagnostic: (event) => { diagnostics.push(event); },
      resolveConversation: async () => ({ send: async () => { throw nativeError; } }),
    })(input, "native-getter"), hasError(502, "DELIVERY_UNCERTAIN"));
    assert.equal(getterReads, 0);
    assertSafeDiagnostic(diagnostics[0]!, "sdk_exception", 14);
  });
});

test("pre-send diagnostic categories preserve safe retry and public errors", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const stage of ["resolution", "preparation"] as const) {
      const previousReceipts = await readdir(deliveryDirectory);
      const diagnostics: SummaryDiagnostic[] = [];
      let sends = 0;
      let fail = true;
      const options = {
        deliveryDirectory,
        diagnostic: (event: SummaryDiagnostic) => { diagnostics.push(event); },
        resolveConversation: async () => {
          if (stage === "resolution" && fail) throw new Error("private resolver details");
          return { send: async () => { sends++; return { id: "safe-retry" }; } };
        },
        prepareReport: async () => {
          if (stage === "preparation" && fail) throw new Error("private preparation details");
        },
      };
      await assert.rejects(createSummarySender(options)(input, `diagnostic-${stage}`), (error: unknown) => {
        assert.ok(error instanceof BridgeError);
        assert.equal(error.status, 503);
        assert.equal(error.body.error.code, stage === "resolution" ? "CONVERSATION_UNAVAILABLE" : "DELIVERY_STORAGE_UNAVAILABLE");
        assert.equal(error.body.error.retryable, stage === "resolution");
        assert.ok(!JSON.stringify(error.body).includes("private"));
        return true;
      });
      assert.equal(sends, 0);
      assert.deepEqual(await readdir(deliveryDirectory), previousReceipts);
      assert.equal(diagnostics.length, 1);
      assertSafeDiagnostic(diagnostics[0]!, stage === "resolution" ? "conversation_resolution_failed" : "report_preparation_failed");
      fail = false;
      assert.equal((await createSummarySender(options)(input, `diagnostic-${stage}`)).status, "accepted");
      assert.equal(sends, 3);
      assert.equal(diagnostics.length, 1);
    }
  });
});

test("diagnostic callback failures never alter ambiguous delivery state or allow resending", async () => {
  await withDirectory(async (deliveryDirectory) => {
    for (const kind of ["throw", "reject"] as const) {
      let sends = 0;
      const options = {
        deliveryDirectory,
        diagnostic: () => {
          if (kind === "throw") throw new Error("private logging failure");
          return Promise.reject(new Error("private logging rejection"));
        },
        resolveConversation: async () => ({ send: async () => { sends++; throw new Error("private send failure"); } }),
      };
      await assert.rejects(createSummarySender(options)(input, `logging-${kind}`), hasError(502, "DELIVERY_UNCERTAIN"));
      await new Promise<void>((resolve) => setImmediate(resolve));
      await assert.rejects(createSummarySender(options)(input, `logging-${kind}`), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.equal(sends, 1);
    }
  });
});

test("default diagnostics log only the fixed safe event", async (context) => {
  const logged: unknown[][] = [];
  context.mock.method(console, "warn", (...values: unknown[]) => { logged.push(values); });
  await withDirectory(async (deliveryDirectory) => {
    const send = createSummarySender({
      deliveryDirectory,
      resolveConversation: async () => ({ send: async () => { throw Object.assign(new Error("private SDK detail"), { code: 14 }); } }),
    });
    await assert.rejects(send(input, "default-diagnostic"), hasError(502, "DELIVERY_UNCERTAIN"));
  });
  assert.equal(logged.length, 1);
  assert.equal(logged[0]![0], "Photon summary diagnostic:");
  assertSafeDiagnostic(JSON.parse(logged[0]![1] as string) as SummaryDiagnostic, "sdk_exception", 14);
  assert.ok(!JSON.stringify(logged).includes("private SDK detail"));
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

const structuredInput: SummaryInput = {
  ...input,
  summary: "Takeaways: The excerpt includes a conditional promise.\n\nWhat this means: The qualifier changes how the post claim reads.\n\nReliability: A source comparison is available, but factual accuracy and intent remain unestablished.",
};

test("three concise messages put reliability first, explanation second, and the report link last", () => {
  const messages = formatSummaryMessages(structuredInput);
  assert.deepEqual(messages, [
    "TL;DR: A source comparison is available, but factual accuracy and intent remain unestablished.",
    "The excerpt includes a conditional promise. The qualifier changes how the post claim reads.",
    `Full report:\n${input.report_url}`,
  ]);
  assert.ok(messages[0].length <= 247);
  assert.ok(messages[1].length <= 650);
  assert.ok(messages.slice(0, 2).every(text => !text.includes(input.report_url)));
  assert.ok(messages.every(text => !text.includes("\u2014")));
});

test("legacy source-match wording does not become a true, false, or authenticity claim", () => {
  const messages = formatSummaryMessages({ ...input, summary: "Matching speech was found in another recording." });
  assert.equal(messages[0], "TL;DR: Reliability is unconfirmed from this summary alone.");
  assert.equal(messages[1], "Matching speech was found in another recording.");
  assert.doesNotMatch(messages.join(" "), /clip is (true|false|accurate|authentic)|delivered|100%/i);
});

test("uncertain source wording remains explicit rather than becoming a negative verdict", () => {
  const messages = formatSummaryMessages({ ...input, summary:
    "Takeaways: The speaker discusses a policy.\n\nWhat this means: The post attributes the speech to a public figure.\n\nReliability: No verified source was found. Context and authenticity remain unassessed." });
  assert.equal(messages[0], "TL;DR: No verified source was found. Context and authenticity remain unassessed.");
  assert.doesNotMatch(messages.join(" "), /misleading|fake|false|fabricated|lying/i);
});

test("text formatting removes em dashes and markup without adding interpretations", () => {
  const messages = formatSummaryMessages({ ...input, summary:
    "Takeaways: A **conditional** claim — with a qualifier.\n\nWhat this means:  Read the condition.\n\nReliability: The source matches — the full event remains unknown." });
  assert.equal(messages[0], "TL;DR: The source matches, the full event remains unknown.");
  assert.equal(messages[1], "A conditional claim, with a qualifier. Read the condition.");
  assert.doesNotMatch(messages.join(" "), /\u2014|\*\*/);
});

test("long explanations stop at complete sentences and long reliability uses a whole neutral fallback", () => {
  const messages = formatSummaryMessages({ ...input, summary:
    `Takeaways: A complete short sentence. ${"longword ".repeat(80)}\n\nWhat this means: See the condition.\n\nReliability: ${"qualified evidence ".repeat(18)}` });
  assert.ok(messages[0].length <= 247);
  assert.equal(messages[0], "TL;DR: The evidence does not establish overall accuracy. Open the report for the assessment and its limits.");
  assert.equal(messages[1], "A complete short sentence.");
  const unbroken = formatSummaryMessages({ ...input, summary: "x".repeat(1000) });
  assert.equal(unbroken[1], "The analysis is longer than a text. Open the report for the full explanation.");
});

test("each multipart acceptance is durably stored before the next provider send", async () => {
  await withDirectory(async deliveryDirectory => {
    const texts: string[] = [];
    const options = { deliveryDirectory, resolveConversation: async () => ({ send: async (text: string) => {
      const [file] = await readdir(deliveryDirectory);
      const ledger = JSON.parse(await readFile(join(deliveryDirectory, file!), "utf8"));
      assert.equal(ledger.state, "pending");
      assert.deepEqual(ledger.message_ids ?? [], texts.map((_text, index) => `part-${index + 1}`));
      texts.push(text);
      return { id: `part-${texts.length}` };
    } }) };
    const result = await createSummarySender(options)(structuredInput, "multipart-order");
    assert.deepEqual(texts, formatSummaryMessages(structuredInput));
    assert.deepEqual(result, { conversation_id: input.conversation_id, message_id: "part-3", status: "accepted" });
    assert.deepEqual(await createSummarySender(options)(structuredInput, "multipart-order"), result);
    assert.equal(texts.length, 3);
    const [file] = await readdir(deliveryDirectory);
    const ledger = JSON.parse(await readFile(join(deliveryDirectory, file!), "utf8"));
    assert.deepEqual(ledger.message_ids, ["part-1", "part-2", "part-3"]);
    assert.equal(ledger.state, "accepted");
  });
});

test("a failure in any multipart message stops the sequence and retries never duplicate parts", async () => {
  await withDirectory(async deliveryDirectory => {
    for (const kind of ["throw", "missing-id"] as const) for (const failurePart of [1, 2, 3]) {
      const texts: string[] = [];
      const options = { deliveryDirectory, diagnostic: () => undefined, resolveConversation: async () => ({ send: async (text: string) => {
        texts.push(text);
        if (texts.length === failurePart) {
          if (kind === "throw") throw new Error("provider detail");
          return undefined;
        }
        return { id: `part-${texts.length}` };
      } }) };
      const job = `multipart-${kind}-${failurePart}`;
      await assert.rejects(createSummarySender(options)(structuredInput, job), hasError(502, "DELIVERY_UNCERTAIN"));
      await assert.rejects(createSummarySender(options)(structuredInput, job), hasError(409, "DELIVERY_UNCERTAIN"));
      assert.deepEqual(texts, formatSummaryMessages(structuredInput).slice(0, failurePart));
      const key = createHash("sha256").update(JSON.stringify([input.conversation_id, job])).digest("hex");
      const ledger = JSON.parse(await readFile(join(deliveryDirectory, `${key}.json`), "utf8"));
      assert.equal(ledger.state, "failed");
      assert.equal((ledger.message_ids ?? []).length, failurePart - 1);
    }
  });
});

test("a stalled second message never sends the report link after late provider success", async () => {
  await withDirectory(async deliveryDirectory => {
    let complete!: (value: { id: string }) => void;
    const stalled = new Promise<{ id: string }>(resolve => { complete = resolve; });
    const texts: string[] = [];
    const options = { deliveryDirectory, sendTimeoutMs: 100, diagnostic: () => undefined,
      resolveConversation: async () => ({ send: async (text: string) => {
        texts.push(text);
        return texts.length === 1 ? { id: "first-accepted" } : stalled;
      } }) };
    await assert.rejects(createSummarySender(options)(structuredInput, "multipart-stalled"), hasError(502, "DELIVERY_UNCERTAIN"));
    complete({ id: "second-too-late" });
    await new Promise<void>(resolve => setImmediate(resolve));
    await assert.rejects(createSummarySender(options)(structuredInput, "multipart-stalled"), hasError(409, "DELIVERY_UNCERTAIN"));
    assert.deepEqual(texts, formatSummaryMessages(structuredInput).slice(0, 2));
  });
});

test("existing accepted single-message receipts remain valid and never replay as multipart", async () => {
  await withDirectory(async deliveryDirectory => {
    const job = "legacy-accepted";
    const key = createHash("sha256").update(JSON.stringify([input.conversation_id, job])).digest("hex");
    await writeFile(join(deliveryDirectory, `${key}.json`), JSON.stringify({ version: 1,
      fingerprint: createHash("sha256").update(JSON.stringify(input)).digest("hex"), state: "accepted", message_id: "old-provider-id" }));
    const send = createSummarySender({ deliveryDirectory, resolveConversation: async () => { throw new Error("Must not resolve or send."); } });
    assert.deepEqual(await send(input, job), { conversation_id: input.conversation_id, message_id: "old-provider-id", status: "accepted" });
  });
});
