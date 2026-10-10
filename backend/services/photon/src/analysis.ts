import { mkdir, open, rename, unlink } from "node:fs/promises";
import { join, resolve } from "node:path";
import type { Space } from "spectrum-ts";
import type { IntakeRecord } from "./receive.ts";

interface Options {
  directory: string;
  backendPort: number;
  request?: typeof fetch;
}

async function notify(space: Space, text: string): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([space.send(text), new Promise<void>((resolve) => { timer = setTimeout(resolve, 12_000); })]);
  } catch { /* An acknowledgement failure never changes persisted processing state. */ }
  finally { if (timer) clearTimeout(timer); }
}

/** Serialize URL investigations; durable markers prohibit accidental automatic replay. */
export function createAnalysisDispatcher(options: Options) {
  if (!Number.isInteger(options.backendPort) || options.backendPort < 1024 || options.backendPort > 65535) {
    throw new Error("Use a valid local backend port.");
  }
  const directory = resolve(options.directory);
  const request = options.request ?? fetch;
  let pending = Promise.resolve();
  let queued = 0;

  async function run(record: IntakeRecord, eventKey: string, space: Space): Promise<void> {
    if (record.input_type !== "url" || !record.input_url || !/^[a-f0-9]{64}$/.test(eventKey)) return;
    const jobId = `photon_${eventKey}`;
    const path = join(directory, `${eventKey}.json`);
    try {
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const marker = await open(path, "wx", 0o600);
      try { await marker.writeFile(JSON.stringify({ job_id: jobId, state: "pending" })); await marker.sync(); }
      finally { await marker.close(); }
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "EEXIST") return;
      await notify(space, "Your request is saved, but analysis could not start because its processing state could not be stored.");
      return;
    }
    let state: "completed" | "uncertain" = "uncertain";
    try {
      const response = await request(`http://127.0.0.1:${options.backendPort}/analyze/${jobId}/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: record.input_url, conversation_id: record.conversation_id }),
        signal: AbortSignal.timeout(30 * 60 * 1000), redirect: "error",
      });
      const result = await response.json() as { job_id?: unknown; report_id?: unknown; delivery_status?: unknown };
      if (!response.ok || result.job_id !== jobId || typeof result.report_id !== "string"
          || !/^report_[A-Za-z0-9_-]{1,128}$/.test(result.report_id)) throw new Error("Analysis did not complete");
      state = "completed";
      if (result.delivery_status !== "accepted") {
        await notify(space, "Your analysis is complete, but the report reply could not be confirmed. Your report remains saved; ask the team to retrieve it.");
      }
    } catch {
      await notify(space, "I could not confirm completion of this analysis. Your request is saved. The team can check its status before retrying.");
    } finally {
      const temporary = `${path}.tmp`;
      try {
        const marker = await open(temporary, "wx", 0o600);
        try { await marker.writeFile(JSON.stringify({ job_id: jobId, state })); await marker.sync(); }
        finally { await marker.close(); }
        await rename(temporary, path);
      } catch { /* Retaining the pending marker prevents an uncertain automatic replay. */ }
      finally { await unlink(temporary).catch(() => undefined); }
    }
  }

  return {
    enqueue(record: IntakeRecord, eventKey: string, space: Space): void {
      if (queued >= 32) {
        void notify(space, "Your request is saved, but the analysis queue is full. Please wait and ask the team to process this saved request.");
        return;
      }
      queued += 1;
      pending = pending.then(() => run(record, eventKey, space)).catch(() => undefined).finally(() => { queued -= 1; });
    },
    idle(): Promise<void> { return pending; },
  };
}
