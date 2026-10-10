import assert from "node:assert/strict";
import test from "node:test";
import type { ReportV2 } from "../lib/api-types.ts";
import { derive, sentenceLookup } from "../lib/view-model.ts";
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
