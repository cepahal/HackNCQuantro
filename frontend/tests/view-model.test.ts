import assert from "node:assert/strict";
import test from "node:test";
import type { ReportV2 } from "../lib/api-types.ts";
import type { ReportDiagnostics } from "../lib/report-diagnostics.ts";
import { derive, sentenceLookup, reportDataMarkers, reportTimelineMarkers } from "../lib/view-model.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

test("derive reports measured source gaps and passage coverage for an aligned report", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.alignment!.matches.push({
    ...report.alignment!.matches[0]!, match_id: "m2", clip_start_seconds: 5, clip_end_seconds: 6,
    source_start_seconds: 108, source_end_seconds: 109,
  });
  report.alignment!.edit_analysis.source_gaps = [{ start_seconds: 104.5, end_seconds: 108, before_match_id: "m2", after_match_id: "m1" }];
  const result = derive(report);
  assert.equal(result.gapSeconds, 3.5);
  assert.equal(result.passageSpan, 8);
  assert.equal(result.leftOutPct, 3.5 / 8);
  assert.equal(result.sourceMatchedSeconds, 4.5);
  assert.equal(result.outOfOrder, false);
});

test("derive leaves unavailable and out-of-order alignment metrics unknown", () => {
  const noAlignment = derive({ ...reportFixture, alignment: null });
  assert.deepEqual(noAlignment, { gapSeconds: null, passageSpan: null, leftOutPct: null, sourceMatchedSeconds: null, outOfOrder: false });

  const outOfOrder = structuredClone(reportFixture) as ReportV2;
  outOfOrder.alignment!.matches.push({
    ...outOfOrder.alignment!.matches[0]!, match_id: "m2", clip_start_seconds: 0, clip_end_seconds: 1,
    source_start_seconds: 108, source_end_seconds: 109,
  });
  const result = derive(outOfOrder);
  assert.equal(result.gapSeconds, null);
  assert.equal(result.outOfOrder, true);
});

test("sentence lookup resolves both canonical IDs and clip/source aliases", () => {
  const transcript = reportFixture.submitted.transcript;
  const sentence = transcript.sentences[0]!;
  assert.equal(sentenceLookup(transcript, sentence.sentence_id), sentence);
  assert.equal(sentenceLookup(transcript, `clip_${sentence.sentence_id}`), sentence);
  assert.equal(sentenceLookup(transcript, `source_${sentence.sentence_id}`), sentence);
  assert.equal(sentenceLookup(transcript, "missing"), undefined);
});


function fixture() { return structuredClone(reportFixture) as ReportV2; }
function marker(value: ReportV2, id: string) { return reportDataMarkers(value).find(m => m.id === id)!; }

test("data markers retain source paths, true zero, unknown null and categorical confidence", () => {
  const value = fixture();
  value.submitted.post.author.followers_count = null;
  value.submitted.post.engagement.likes_count = 0;
  const before = JSON.stringify(value);
  assert.equal(marker(value,"submitted.post.author.followers_count").value,null);
  assert.equal(marker(value,"submitted.post.engagement.likes_count").value,0);
  assert.equal(marker(value,"alignment.match_quality.clip_coverage_ratio").value,0.5);
  assert.deepEqual(marker(value,"alignment.match_quality.clip_coverage_ratio").range,[0,1]);
  const confidence = marker(value,"alignment.match_quality.confidence.level");
  assert.equal(confidence.value,"moderate");
  assert.equal(confidence.range,null);
  assert.equal(confidence.origin,"model");
  assert.equal(JSON.stringify(value),before);
  assert.equal(new Set(reportDataMarkers(value).map(m => m.id)).size,reportDataMarkers(value).length);
});

test("merged transcript ranges do not double count overlapping windows", () => {
  const value = fixture();
  value.submitted.transcript.processed_ranges = [{start_seconds:0,end_seconds:10},{start_seconds:5,end_seconds:15}];
  assert.equal(marker(value,"submitted.transcript.processed_seconds").value,15);
  value.submitted.transcript.processed_ranges[0]!.end_seconds = Number.NaN;
  assert.equal(marker(value,"submitted.transcript.processed_seconds").value,null);
});

test("unassessed edits and stale no-match fields never become measured chart values", () => {
  const value = fixture();
  value.alignment!.edit_analysis.status = "not_assessed";
  assert.equal(marker(value,"alignment.edit_analysis.source_gap_seconds").value,null);
  assert.equal(marker(value,"alignment.edit_analysis.counts.confirmed_deleted_segments").value,null);
  value.outcome = "no_verified_match";
  assert.equal(marker(value,"alignment.match_quality.clip_coverage_ratio").value,null);
  assert.equal(marker(value,"comparison.context_assessment").value,null);
  assert.equal(marker(value,"source.frames.length").value,1); // recorded data is retained
  assert.deepEqual(reportTimelineMarkers(value),[]);
});

test("timeline markers bind passages to both recordings and omit invalid ranges", () => {
  const value = fixture();
  const markers = reportTimelineMarkers(value);
  assert.equal(markers.filter(m => m.kind === "match").length,2);
  assert.ok(markers.some(m => m.kind === "unmatched" && m.track === "submitted"));
  assert.ok(markers.some(m => m.kind === "context" && m.path === "context.windows[0]"));
  value.alignment!.matches[0]!.clip_end_seconds = -1;
  assert.ok(!reportTimelineMarkers(value).some(m => m.id === "clip-m1"));
});

test("old reports and mismatched diagnostics never invent provider usage or timing", () => {
  const value = fixture();
  assert.ok(!reportDataMarkers(value).some(m => m.path.startsWith("diagnostics")));
  const diag = {job_id:"other",capture_status:"recorded" as const,events:[],terminal_output:""};
  assert.ok(!reportDataMarkers(value,diag).some(m => m.path.startsWith("diagnostics")));
  assert.ok(!reportDataMarkers(value).some(m => /token|cost|accuracy|deepfake/i.test(m.label) && m.range));
});

test("provider scores and timing scopes have distinct units and no probability labels", () => {
  const value = fixture();
  const diag: ReportDiagnostics = {job_id:value.job_id,capture_status:"recorded" as const,terminal_output:"",events:[
    {stage:"candidate_transcript",status:"scored",elapsed_seconds:2,timing_kind:"stage_duration" as const,data:{match_score:87.5}},
    {stage:"run_analysis",status:"completed",elapsed_seconds:6,timing_kind:"job_elapsed" as const,data:{}},
  ]};
  const rows = reportDataMarkers(value,diag);
  const score = rows.find(m => m.id.endsWith("data.match_score"))!;
  assert.equal(score.unit,"score_0_100");
  assert.equal(score.value,87.5);
  assert.ok(rows.some(m => m.label.includes("stage_duration")));
  assert.ok(rows.some(m => m.label.includes("job_elapsed")));
  assert.equal(rows.filter(m => m.path.endsWith("elapsed_seconds")).length,2);
});

test("unknown export values and invalid bounded scores stay unavailable", () => {
  const value = fixture();
  value.context_selection = {start_seconds:"not a number",end_seconds:20,strategy:"nearest_two_sentences"};
  value.alignment!.match_quality.clip_coverage_ratio = 5;
  assert.equal(marker(value,"context_selection.start_seconds").value,null);
  assert.equal(marker(value,"alignment.match_quality.clip_coverage_ratio").value,null);
  assert.equal(marker(value,"context_selection.strategy").value,"nearest_two_sentences");
});
