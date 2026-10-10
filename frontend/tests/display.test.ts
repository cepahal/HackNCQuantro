import assert from "node:assert/strict";
import test from "node:test";
import type { ReportV2 } from "../lib/api-types.ts";
import { CONFIDENCE_NOTE, confidenceDisplay, displayCount, NO_SOURCE_MESSAGE, posterCard } from "../lib/display.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

const noMatch: ReportV2 = {
  ...reportFixture, outcome: "no_verified_match", source: null, alignment: null, context: null, comparison: null, summary: null,
};

test("a matched report shows three labelled blocks with level words and a rounded coverage percentage", () => {
  const display = confidenceDisplay(reportFixture);
  assert.deepEqual(display.source_match, {
    label: "Source-match confidence", value: "Moderate", basis: ["Two aligned sentences."], limitations: [],
  });
  assert.deepEqual(display.clip_coverage, {
    label: "Submitted clip aligned", value: "50%", description: "3.5 seconds aligned to the selected source.",
  });
  assert.equal(display.context_assessment?.label, "Context-assessment confidence");
  assert.equal(display.context_assessment?.value, "Moderate");
  assert.equal(display.confidence_note, CONFIDENCE_NOTE);
  assert.equal(display.no_source_message, null);
});

test("no verified match hides the source-match and coverage blocks", () => {
  const display = confidenceDisplay(noMatch);
  assert.equal(display.source_match, null);
  assert.equal(display.clip_coverage, null);
  assert.equal(display.no_source_message, NO_SOURCE_MESSAGE);
  assert.equal(display.context_assessment?.value, "Not assessed");
});

test("not_assessed levels read 'Not assessed' and confidence is never a percentage or an accuracy claim", () => {
  const notAssessed = structuredClone(reportFixture) as ReportV2;
  notAssessed.alignment!.match_quality.confidence.level = "not_assessed";
  notAssessed.comparison!.assessment_confidence.level = "not_assessed";
  const display = confidenceDisplay(notAssessed);
  assert.equal(display.source_match?.value, "Not assessed");
  assert.equal(display.context_assessment?.value, "Not assessed");
  for (const block of [display.source_match, display.context_assessment]) assert.doesNotMatch(block?.value ?? "", /%/);
  assert.doesNotMatch(JSON.stringify(display).toLowerCase(), /100% accurate/);
});

test("null counts and poster fields are never shown as 0", () => {
  assert.equal(displayCount(null, "Not assessed"), "Not assessed");
  assert.equal(displayCount(0, "Not assessed"), "0");
  const card = posterCard({ ...reportFixture.submitted.post.author, handle: null, followers_count: null, verified: null });
  assert.deepEqual(card, { handle: "Unavailable", followers_count: "Unavailable", verified: "Unavailable" });
});
