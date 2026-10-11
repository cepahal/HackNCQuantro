import assert from "node:assert/strict";
import test from "node:test";
import { reportAnalytics } from "../lib/report-analytics.ts";
import { reportFixture } from "./fixtures/report_v2.ts";
import type { ReportV2 } from "../lib/api-types.ts";

const report = (): ReportV2 => structuredClone(reportFixture);

test("analytics use source confidence, exact token counts, and recorded context ranges", () => {
  const data = reportAnalytics(report());
  assert.equal(data.isVideoDetected, true);
  assert.equal(data.wafer.confidence, "Moderate");
  assert.deepEqual(data.wafer.wordMatches, { matched: 5, total: 8 });
  assert.deepEqual(data.metrics.contextWindows, [{ start: 71, end: 134.5 }]);
  assert.equal(data.metrics.generationSeconds, null);
  assert.equal(data.wafer.sourceGaps, 0);
});

test("generation time uses completed job elapsed time, never the sum of overlapping stage times", () => {
  const base = { job_id: "job_1", capture_status: "recorded" as const, terminal_output: "" };
  const data = reportAnalytics(report(), { ...base, events: [
    { stage: "retrieve_post", status: "completed", elapsed_seconds: 10, timing_kind: "stage_duration", data: {} },
    { stage: "run_analysis", status: "completed", elapsed_seconds: 12, timing_kind: "job_elapsed", data: {} },
    { stage: "report_interpretation", status: "ready", elapsed_seconds: 4, timing_kind: "stage_duration", data: {} },
  ] });
  assert.equal(data.metrics.generationSeconds, 12);
  assert.equal(reportAnalytics(report(), { ...base, events: [{ stage: "run_analysis", status: "completed", elapsed_seconds: 12, data: {} }] }).metrics.generationSeconds, null);
});

test("unknown counts and unassessed edits stay unavailable rather than zero", () => {
  const value = report();
  value.alignment!.edit_analysis.status = "not_assessed";
  value.alignment!.edit_analysis.counts.source_gaps = null;
  value.submitted.transcript.sentences = [];
  const data = reportAnalytics(value);
  assert.equal(data.wafer.sourceGaps, null);
  assert.equal(data.wafer.wordMatches, null);
});

for (const problem of ["no_match", "wrong_source", "wrong_clip", "low_confidence", "no_passages"]) {
  test(`${problem} suppresses the confident analytics view`, () => {
    const value = report();
    if (problem === "no_match") value.outcome = "no_verified_match";
    if (problem === "wrong_source") value.alignment!.source_media_id = "wrong";
    if (problem === "wrong_clip") value.alignment!.clip_media_id = "wrong";
    if (problem === "low_confidence") value.alignment!.match_quality.confidence.level = "low";
    if (problem === "no_passages") value.alignment!.matches = [];
    const data = reportAnalytics(value);
    assert.equal(data.isVideoDetected, false);
    assert.equal(data.wafer.wordMatches, null);
    assert.deepEqual(data.metrics.contextWindows, []);
    assert.equal(data.wafer.sourceGaps, null);
  });
}

test("repeated words cannot multiply a source token into several word matches", () => {
  const value = report();
  value.source!.transcript.sentences[0].text = "hello";
  value.submitted.transcript.sentences = [{ ...value.submitted.transcript.sentences[0], text: "hello hello hello" }];
  assert.deepEqual(reportAnalytics(value).wafer.wordMatches, { matched: 1, total: 3 });
});

test("projection never mutates the saved report or converts fuzzy similarity to accuracy", () => {
  const value = report();
  const before = JSON.stringify(value);
  const data = reportAnalytics(value);
  assert.equal(JSON.stringify(value), before);
  assert.doesNotMatch(JSON.stringify(data), /95%|92%|100% accurate|Very high|accuracy/);
});
