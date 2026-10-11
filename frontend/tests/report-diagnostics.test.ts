import assert from "node:assert/strict";
import test from "node:test";
import { analysisName, diagnosticFilename, diagnosticOutput, type ReportDiagnostics } from "../lib/report-diagnostics.ts";
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
  assert.deepEqual(saved.report, report);
  assert.deepEqual(saved.diagnostics, diagnostics);
  assert.equal(saved.display, null);
  assert.ok(saved.data_markers.some((m: {id:string}) => m.id === "alignment.match_quality.clip_coverage_ratio"));
  assert.ok(saved.timeline_markers.some((m: {kind:string}) => m.kind === "match"));
  assert.equal(saved.report.submitted.post.author.followers_count, null);
});

test("historical or missing data never invents terminal output", () => {
  const text = diagnosticOutput(null, null, null, "Output was not recorded.", "job_failed");
  assert.match(text, /Job: job_failed/);
  assert.match(text, /Outcome: Report unavailable/);
  assert.match(text, /Output was not recorded/);
  assert.doesNotMatch(text, /python main.py|completed successfully/);
});

test("a submitted video title names the output and download without the job identifier", () => {
  const report = structuredClone(reportFixture);
  report.submitted.post.title = "  Obama Morehouse Speech  ";
  assert.equal(analysisName(report), "Obama Morehouse Speech");
  assert.equal(diagnosticFilename(report), "obama-morehouse-speech-analysis.txt");
  const text = diagnosticOutput(report, null, null, "Output was not recorded.");
  assert.equal(text.split("\n")[0], "Obama Morehouse Speech | ANALYSIS DIAGNOSTICS");
  assert.ok(text.includes(`Job: ${report.job_id}`));
  assert.ok(!diagnosticFilename(report).includes(report.job_id));
});

test("missing, blank and punctuation-only titles use a readable fallback", () => {
  assert.equal(analysisName(null), "Video analysis");
  assert.equal(diagnosticFilename(null), "video-analysis.txt");
  for (const title of [null, "", "  ", "... / ?"]) {
    const report = structuredClone(reportFixture);
    report.submitted.post.title = title;
    assert.equal(analysisName(report), "Video analysis");
    assert.equal(diagnosticFilename(report), "video-analysis.txt");
  }
});

test("internal identifiers and UUID-looking titles never become analysis names", () => {
  for (const title of ["job_1791705481", "report_job_123", "REPORT: 123", "job", "report",
    "01a126ae-8d44-76b3-855d-7bd837d0a072", "{01a126ae-8d44-76b3-855d-7bd837d0a072}",
    "01a126ae8d4476b3855d7bd837d0a072", reportFixture.job_id]) {
    const report = structuredClone(reportFixture);
    report.submitted.post.title = title;
    assert.equal(analysisName(report), "Video analysis", title);
    assert.equal(diagnosticFilename(report), "video-analysis.txt", title);
  }
});

test("download names remove path separators and unsafe punctuation while retaining useful words", () => {
  const report = structuredClone(reportFixture);
  report.submitted.post.title = '  Obama / Morehouse: "Speech"? <2024> | ..\\  ';
  assert.equal(diagnosticFilename(report), "obama-morehouse-speech-2024-analysis.txt");
  report.submitted.post.title = "Café climate analysis";
  assert.equal(analysisName(report), "Café climate analysis");
  assert.equal(diagnosticFilename(report), "cafe-climate-analysis.txt");
  report.submitted.post.title = "Job creation";
  assert.equal(analysisName(report), "Job creation");
});

test("long and non-Latin video titles still produce bounded safe download names", () => {
  const report = structuredClone(reportFixture);
  report.submitted.post.title = "A long speech ".repeat(100);
  const filename = diagnosticFilename(report);
  assert.ok(filename.length <= 113);
  assert.match(filename, /^[a-z0-9]+(?:-[a-z0-9]+)*-analysis\.txt$/);
  report.submitted.post.title = "議会演説";
  assert.equal(analysisName(report), "議会演説");
  assert.equal(diagnosticFilename(report), "video-analysis.txt");
});
