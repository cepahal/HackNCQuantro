import { randomUUID } from "node:crypto";
import { lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname, isAbsolute, parse, relative, resolve, sep } from "node:path";
import type { SummaryInput } from "./summary.ts";

const updates = new Map<string, Promise<void>>();
const reportIdPattern = /^report_[A-Za-z0-9_-]{1,128}$/;
const configurationError = (): Error => new Error("Public report sharing requires a regular file inside PHOTON_DATA_DIR and a valid REPORT_BASE_URL.");

function within(directory: string, file: string): boolean {
  const path = relative(directory, file);
  return path !== "" && path !== ".." && !path.startsWith(`..${sep}`) && !isAbsolute(path);
}

async function checkPath(file: string): Promise<void> {
  const root = parse(file).root;
  let current = root;
  const parts = file.slice(root.length).split(sep);
  for (const [index, part] of parts.entries()) {
    current = resolve(current, part);
    let stat;
    try {
      stat = await lstat(current);
    } catch (error) {
      if (error instanceof Error && "code" in error && error.code === "ENOENT") return;
      throw configurationError();
    }
    if (stat.isSymbolicLink() || (index < parts.length - 1 ? !stat.isDirectory() : !stat.isFile())) {
      throw configurationError();
    }
  }
}

export async function createPublicReportPreparer(options: {
  directory: string;
  file: string;
  reportBaseUrl: string;
}): Promise<(input: SummaryInput, jobId: string) => Promise<void>> {
  const directory = resolve(options.directory);
  const file = resolve(options.file);
  if (!within(directory, file)) throw configurationError();
  let base: URL;
  try {
    base = new URL(options.reportBaseUrl);
  } catch {
    throw configurationError();
  }
  if (!["https:", "http:"].includes(base.protocol) || base.username || base.password) throw configurationError();
  await checkPath(file);

  return async (input, jobId): Promise<void> => {
    const id = `report_${jobId}`;
    const url = new URL(input.report_url);
    if (
      !reportIdPattern.test(id) || url.origin !== base.origin || url.pathname !== `/reports/${id}`
      || url.username || url.password || url.search || url.hash
    ) throw new Error("The public report URL must match this job and REPORT_BASE_URL exactly.");

    const prior = updates.get(file) ?? Promise.resolve();
    const update = prior.then(async () => {
      await checkPath(file);
      let ids: string[] = [];
      try {
        const stat = await lstat(file);
        if (stat.size > 16_384) throw new Error("Public report allowlist is too large.");
        const value: unknown = JSON.parse(await readFile(file, "utf8"));
        if (!Array.isArray(value) || value.length > 32 || value.some((item) => typeof item !== "string" || !reportIdPattern.test(item))) {
          throw new Error("Public report allowlist is invalid.");
        }
        ids = value as string[];
      } catch (error) {
        if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) throw error;
      }
      if (ids.includes(id)) return;
      if (ids.length >= 32) throw new Error("Public report allowlist is full.");
      await mkdir(dirname(file), { recursive: true });
      await checkPath(file);
      const temporary = `${file}.${randomUUID()}.tmp`;
      try {
        const handle = await open(temporary, "wx", 0o600);
        try {
          await handle.writeFile(JSON.stringify([...new Set([...ids, id])]), "utf8");
          await handle.sync();
        } finally {
          await handle.close();
        }
        await rename(temporary, file);
      } finally {
        await unlink(temporary).catch(() => undefined);
      }
    });
    const tail = update.catch(() => undefined);
    updates.set(file, tail);
    try {
      await update;
    } finally {
      if (updates.get(file) === tail) updates.delete(file);
    }
  };
}
