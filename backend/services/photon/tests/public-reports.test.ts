import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createPublicReportPreparer } from "../src/public-reports.ts";

const origin = "https://quantro.example";
const input = (jobId: string) => ({ conversation_id: "chat-1", summary: "Evidence found.", report_url: `${origin}/reports/report_${jobId}` });

async function withDirectory(run: (directory: string, file: string) => Promise<void>): Promise<void> {
  const directory = await mkdtemp(join(tmpdir(), "quantro-public-reports-"));
  try {
    await run(directory, join(directory, "public-reports.json"));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("public allowlist rejects origin, exact path, credentials and job mismatches before writing", async () => {
  await withDirectory(async (directory, file) => {
    const prepare = await createPublicReportPreparer({ directory, file, reportBaseUrl: origin });
    for (const report_url of [
      "https://other.example/reports/report_job_1", `${origin}/reports/report_other`,
      `${origin}/reports/report_job_1/`, `${origin}/reports/report_job_1?extra=1`,
      `${origin}/reports/report_job_1#extra`, "https://user:secret@quantro.example/reports/report_job_1",
    ]) await assert.rejects(prepare({ ...input("job_1"), report_url }, "job_1"));
    await assert.rejects(prepare(input("../escape"), "../escape"));
    assert.deepEqual(await readdir(directory), []);
  });
});

test("concurrent report preparations atomically merge IDs and preserve existing entries", async () => {
  await withDirectory(async (directory, file) => {
    await writeFile(file, JSON.stringify(["report_existing"]));
    const options = { directory, file, reportBaseUrl: origin };
    const first = await createPublicReportPreparer(options);
    const second = await createPublicReportPreparer(options);
    await Promise.all(Array.from({ length: 24 }, (_, index) => (index % 2 ? first : second)(input(`job_${index}`), `job_${index}`)));
    await first(input("job_1"), "job_1");
    const ids = JSON.parse(await readFile(file, "utf8")) as string[];
    assert.equal(ids.length, 25);
    assert.ok(ids.includes("report_existing"));
    for (let index = 0; index < 24; index++) assert.ok(ids.includes(`report_job_${index}`));
    assert.deepEqual(await readdir(directory), ["public-reports.json"]);
  });
});

test("missing allowlist is created while malformed or invalid existing data fails closed", async () => {
  await withDirectory(async (directory, file) => {
    const prepare = await createPublicReportPreparer({ directory, file, reportBaseUrl: origin });
    await prepare(input("job_new"), "job_new");
    assert.deepEqual(JSON.parse(await readFile(file, "utf8")), ["report_job_new"]);
    for (const raw of ["not JSON", "{}", '["../private"]', '["report_good",null]']) {
      await writeFile(file, raw);
      await assert.rejects(prepare(input("job_new"), "job_new"));
      assert.equal(await readFile(file, "utf8"), raw);
    }
  });
});

test("outside paths and symlinked ancestors are rejected at startup", async () => {
  await withDirectory(async (directory, file) => {
    await assert.rejects(createPublicReportPreparer({ directory, file: join(directory, "..", "outside.json"), reportBaseUrl: origin }));
    await assert.rejects(createPublicReportPreparer({ directory, file, reportBaseUrl: "file:///private" }));
    const target = await mkdtemp(join(tmpdir(), "quantro-public-target-"));
    try {
      const link = join(directory, "linked");
      await symlink(target, link, process.platform === "win32" ? "junction" : "dir");
      await assert.rejects(createPublicReportPreparer({ directory, file: join(link, "public.json"), reportBaseUrl: origin }));
    } finally {
      await rm(target, { recursive: true, force: true });
    }
  });
});

test("allowlist limits preserve existing published links and reject a thirty-third report", async () => {
  await withDirectory(async (directory, file) => {
    const prepare = await createPublicReportPreparer({ directory, file, reportBaseUrl: origin });
    const ids = Array.from({ length: 32 }, (_, index) => `report_job_${index}`);
    const raw = JSON.stringify(ids);
    await writeFile(file, raw);
    await prepare(input("job_1"), "job_1");
    await assert.rejects(prepare(input("job_33"), "job_33"));
    assert.equal(await readFile(file, "utf8"), raw);
    for (const invalid of [JSON.stringify([...ids, "report_extra"]), `${raw}${" ".repeat(16_384)}`]) {
      await writeFile(file, invalid);
      await assert.rejects(prepare(input("job_1"), "job_1"));
      assert.equal(await readFile(file, "utf8"), invalid);
    }
  });
});
