import assert from "node:assert/strict";
import test from "node:test";
import { diagnosticOutput, type ReportDiagnostics } from "../lib/report-diagnostics.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

test("diagnostic output retains all nested data, nulls and the entire captured output", () => {
  const report = structuredClone(reportFixture);
  report.summary = "Full transcript ".repeat(1000);
  report.submitted.post.author.followers_count = null;
  const diagnostics: ReportDiagnostics = { job_id: report.job_id, capture_status: "recorded",
    events: [{stage:"search_candidates",status:"completed",elapsed_seconds:2,data:{scores:[85.5,74],unknown:null}}],
    terminal_output:"ACTUAL RECORDED EVENT\n" + "event data ".repeat(300),
  };
  const text = diagnosticOutput(report, null, diagnostics, "loaded");
  assert.ok(text.includes(diagnostics.terminal_output));
  const saved = JSON.parse(text.split("=== ALL SAVED ANALYSIS DATA (JSON) ===\n")[1]!);
  assert.deepEqual(saved, {report,display:null,diagnostics});
  assert.equal(saved.report.submitted.post.author.followers_count, null);
});

test("historical or missing data never invents terminal output", () => {
  const text = diagnosticOutput(null, null, null, "Output was not recorded.", "job_failed");
  assert.match(text, /Job: job_failed/);
  assert.match(text, /Outcome: Report unavailable/);
  assert.match(text, /Output was not recorded/);
  assert.doesNotMatch(text, /python main.py|completed successfully/);
});
