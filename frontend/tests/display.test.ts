import assert from "node:assert/strict";
import test from "node:test";
import type { ReportV2 } from "../lib/api-types.ts";
import { QuantroApiError } from "../lib/api.ts";
import {
  CONFIDENCE_NOTE, confidenceDisplay, displayCount, errorDisplay, generatedSummary, NO_SOURCE_MESSAGE, posterCard, reportSummary,
  secondsLabel,
} from "../lib/display.ts";
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

test("matched reports without alignment do not claim that no source was found", () => {
  const report = { ...reportFixture, alignment: null, summary: null, comparison: null };
  const display = confidenceDisplay(report);
  assert.equal(display.no_source_message, null);
  assert.equal(display.source_match, null);
  assert.match(generatedSummary(report), /^A source recording was matched, but alignment details are unavailable\./);
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

test("the report paragraph prefers the backend summary, then the comparison summary", () => {
  assert.equal(reportSummary(reportFixture), reportFixture.summary);
  assert.equal(reportSummary({ ...reportFixture, summary: null }), reportFixture.comparison.summary);
});

test("without written summaries a matched report gets a deterministic paragraph from its fields", () => {
  const withoutSummaries = structuredClone(reportFixture) as ReportV2;
  withoutSummaries.summary = null;
  withoutSummaries.comparison!.summary = "";
  const paragraph = reportSummary(withoutSummaries);
  assert.equal(paragraph, generatedSummary(withoutSummaries));
  assert.equal(paragraph, [
    "A source recording was matched with moderate source-match confidence.",
    "50% of the submitted clip (3.5 seconds) aligned to the selected source.",
    "The surrounding source context suggests material context is missing from the submitted clip.",
    "The caption appears contradicted by the source context.",
    "Context-assessment confidence is moderate.",
    "Key finding: The source continues with a condition.",
    "The report lists 1 limitation.",
  ].join(" "));
});

test("the no-match paragraph states the absence of a source without claiming accuracy", () => {
  const paragraph = generatedSummary(noMatch);
  assert.match(paragraph, new RegExp(`^${NO_SOURCE_MESSAGE} for this post\\. That does not establish`));
  assert.equal(reportSummary(noMatch), paragraph);
  assert.doesNotMatch(paragraph, /\d+%|100% accurate|confidence is high/i);
});

test("not-assessed confidence never becomes a level word or a percentage in the paragraph", () => {
  const notAssessed = structuredClone(reportFixture) as ReportV2;
  notAssessed.summary = null;
  notAssessed.comparison = null;
  notAssessed.alignment!.match_quality.confidence.level = "not_assessed";
  notAssessed.alignment!.match_quality.clip_coverage_ratio = null;
  assert.equal(generatedSummary(notAssessed),
    "A source recording was matched; source-match confidence was not assessed. The report lists 1 limitation.");
});

test("generated summaries use the capped coverage percentage formatter", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.summary = null;
  report.comparison!.summary = "";
  report.alignment!.match_quality.clip_coverage_ratio = 0.996;
  assert.match(generatedSummary(report), /99% of the submitted clip/);
  assert.doesNotMatch(generatedSummary(report), /100% of the submitted clip/);
});

test("seconds labels round to a tenth and keep unknowns as 'Not assessed'", () => {
  assert.equal(secondsLabel(19.063583), "19.1s");
  assert.equal(secondsLabel(0), "0s");
  assert.equal(secondsLabel(null), "Not assessed");
  assert.equal(secondsLabel(undefined), "Not assessed");
});

test("errors render as code, stage and message for the report error card", () => {
  assert.deepEqual(errorDisplay(new QuantroApiError("Search is unavailable.", 502, "SEARCH_FAILED", "search_candidates", true)),
    { error: { code: "SEARCH_FAILED", stage: "search_candidates", message: "Search is unavailable.", retryable: true } });
  assert.deepEqual(errorDisplay(new QuantroApiError("Report not found", 404, null, null, false)),
    { error: { code: "HTTP 404", stage: "unknown", message: "Report not found", retryable: false } });
  assert.deepEqual(errorDisplay(new Error("Could not reach the analysis service.")),
    { error: { code: "REQUEST_FAILED", stage: "unknown", message: "Could not reach the analysis service.", retryable: true } });
  const envelope = { error: { code: "MISSING_REPORT", stage: "load_report", message: "Choose a report to view.", retryable: false } };
  assert.deepEqual(errorDisplay(envelope), envelope);
  assert.equal(errorDisplay("unexpected").error.message, "The request could not be completed.");
});
