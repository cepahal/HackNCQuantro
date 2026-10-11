import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReportV2 } from "../lib/api-types.ts";
import { reportSummary } from "../lib/display.ts";
import { buildReportSummary, summaryText, summaryQuoteLabel } from "../lib/report-summary.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

const prefix = "Assuming this is political speech, a tentative reading of the submitted transcript is: ";
const limit = "Factual accuracy and deceptive intent are not established by this partial transcript.";

function interpreted(): ReportV2 {
  const report = structuredClone(reportFixture) as ReportV2;
  report.submitted.transcript.status = "partial";
  report.summary = prefix + "The excerpt discusses a tax policy and a condition. "
    + "Transcript evidence: “We will not raise taxes.” / “Unless we must.” "
    + "Possible references (unverified hypotheses only): A public budget debate. "
    + "The caption's relationship to this transcript is uncertain. "
    + limit + " Existing pipeline result: A distinctive speech passage matches a candidate recording. "
    + "Partial transcript context: The source includes a condition.";
  return report;
}

test("known backend format separates all six meanings without implementation prefixes", () => {
  const report = interpreted();
  const view = buildReportSummary(report);
  assert.deepEqual(view.sections.map(s => s.id), ["meaning", "evidence", "references", "caption", "finding", "limits"]);
  assert.equal(view.politicalAssumption, true);
  assert.equal(view.partialTranscript, true);
  assert.deepEqual(view.sections[0].paragraphs, ["The excerpt discusses a tax policy and a condition."]);
  assert.equal(view.sections[0].note, "We are treating this as political speech; that has not been verified.");
  const finding = view.sections.find(s => s.id === "finding")!;
  assert.deepEqual(finding.paragraphs, [
    "A distinctive speech passage matches a candidate recording.",
    "Partial transcript context: The source includes a condition.",
  ]);
  assert.match(finding.note!, /does not establish the earliest original/);
  assert.doesNotMatch(JSON.stringify(view), /Existing pipeline result:/);
  assert.ok(view.sections.at(-1)!.paragraphs.includes(limit));
});

test("blockquotes come from complete actual transcript sentences with their supplied bounds", () => {
  const report = interpreted();
  const observed = buildReportSummary(report).sections.find(s => s.id === "evidence")!;
  assert.deepEqual(observed.quotes, report.submitted.transcript.sentences);
  assert.deepEqual(observed.paragraphs, []);
  assert.match(observed.note!, /wording and timing may still contain errors/);
});

test("quote display respects summary order without changing transcript evidence", () => {
  const report = interpreted();
  report.summary = report.summary!.replace("“We will not raise taxes.” / “Unless we must.”", "“Unless we must.” / “We will not raise taxes.”");
  const quotes = buildReportSummary(report).sections.find(s => s.id === "evidence")!.quotes;
  assert.deepEqual(quotes.map(q => q.text), ["Unless we must.", "We will not raise taxes."]);
});

for (const quote of ["An invented sentence.", "We will not raise", "we will not raise taxes."]) {
  test(`unlinked or altered quote stays labeled prose, never a verified blockquote: ${quote}`, () => {
    const report = interpreted();
    report.summary = report.summary!.replace("“We will not raise taxes.” / “Unless we must.”", `“${quote}”`);
    const observed = buildReportSummary(report).sections.find(s => s.id === "evidence")!;
    assert.deepEqual(observed.quotes, []);
    assert.match(observed.paragraphs.join(" "), /Unlinked wording/);
    assert.ok(observed.paragraphs.join(" ").includes(quote));
    assert.match(observed.note!, /could not be uniquely linked/);
  });
}

test("repeated transcript wording does not invent a unique citation or timestamp", () => {
  const report = interpreted();
  report.submitted.transcript.sentences.push({ ...report.submitted.transcript.sentences[0], sentence_id: "repeated", start_seconds: 20, end_seconds: 23.5 });
  const observed = buildReportSummary(report).sections.find(s => s.id === "evidence")!;
  assert.deepEqual(observed.quotes.map(q => q.text), ["Unless we must."]);
  assert.match(observed.paragraphs.join(" "), /We will not raise taxes/);
});

test("unverified references keep their exact wording and visible uncertainty", () => {
  const references = buildReportSummary(interpreted()).sections.find(s => s.id === "references")!;
  assert.match(references.title, /Unverified/);
  assert.deepEqual(references.paragraphs, ["A public budget debate."]);
  assert.match(references.note!, /not established events or verified facts/);
});

test("ordinary and older summaries remain complete plain paragraphs, even with marker words", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.summary = "Transcript evidence: This is arbitrary user prose.\n\nExisting pipeline result: Preserve this second paragraph.";
  const view = buildReportSummary(report);
  assert.equal(view.politicalAssumption, false);
  assert.deepEqual(view.sections.map(s => s.id), ["summary"]);
  assert.deepEqual(view.sections[0].paragraphs, report.summary.split("\n\n"));
  assert.deepEqual(view.sections[0].quotes, []);
});

test("an incomplete interpretation frame falls back without dropping text", () => {
  const report = interpreted();
  report.summary = prefix + "No fixed limits footer was supplied. Transcript evidence: “Unless we must.”";
  const view = buildReportSummary(report);
  assert.deepEqual(view.sections[0].paragraphs, [report.summary]);
  assert.equal(view.politicalAssumption, false);
});

test("no-match and missing-caption reports do not manufacture a source or caption relation", () => {
  const report = interpreted();
  report.outcome = "no_verified_match";
  report.source = report.alignment = report.context = report.comparison = null;
  report.submitted.post.caption = null;
  report.summary = prefix + "The supplied words concern a tax policy. " + limit
    + " Existing pipeline result: No distinctive unique speech match was verified.";
  const view = buildReportSummary(report);
  assert.equal(view.sections.some(s => s.id === "caption" || s.id === "evidence"), false);
  const finding = view.sections.find(s => s.id === "finding")!;
  assert.deepEqual(finding.paragraphs, ["No distinctive unique speech match was verified."]);
  assert.equal(finding.note, undefined);
});

test("unexpected trailing prose and HTML-like strings remain plain text", () => {
  const report = interpreted();
  report.summary = prefix + "The text says <script>alert(1)</script>. " + limit + " Preserve this extra qualification.";
  const view = buildReportSummary(report);
  assert.ok(view.sections[0].paragraphs[0].includes("<script>alert(1)</script>"));
  assert.ok(view.sections.find(s => s.id === "limits")!.paragraphs.includes("Preserve this extra qualification."));
});

test("null summaries use the existing fallback unchanged and never mutate the report", () => {
  const report = interpreted();
  const before = JSON.stringify(report);
  buildReportSummary(report);
  assert.equal(JSON.stringify(report), before);
  report.summary = null;
  assert.deepEqual(buildReportSummary(report).sections[0].paragraphs, [reportSummary(report)]);
  report.comparison = null;
  assert.deepEqual(buildReportSummary(report).sections[0].paragraphs, [reportSummary(report)]);
});

test("summary typography removes bold markers and em dashes; HTML stays escaped text", () => {
  const text = "A **clear** reading — **<script>alert(1)</script>** <img src=x onerror=alert(2)>";
  const html = renderToStaticMarkup(createElement("p", null, summaryText(text)));
  assert.match(html, /A clear reading, &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(2\)&gt;/);
  assert.doesNotMatch(html, /<strong|\*\*|\u2014|<script|<img|dangerouslySetInnerHTML/);
});

test("unmatched bold markers are removed while links and HTML remain plain text", () => {
  const text = "**unfinished [link](javascript:alert(1)) <b>word</b>";
  const html = renderToStaticMarkup(createElement("p", null, summaryText(text)));
  assert.ok(html.includes("unfinished [link](javascript:alert(1))"));
  assert.ok(html.includes("&lt;b&gt;word&lt;/b&gt;"));
  assert.doesNotMatch(html, /\*\*|<a|<b>|<strong/);
});

test("long old model readings are preserved in disclosure while factual status stays available", () => {
  const report = interpreted();
  const originalMeaning = Array.from({ length: 80 }, (_, index) => `word${index}`).join(" ");
  report.summary = prefix + originalMeaning + " " + limit;
  const summary = buildReportSummary(report);
  const meaning = summary.sections.find(section => section.id === "meaning")!;
  assert.equal(meaning.deferredReading, true);
  assert.deepEqual(meaning.paragraphs, [originalMeaning]);
  assert.match(summary.evidenceStatus[0].text, /Matching speech was found/);
  assert.match(summary.evidenceStatus[0].text, /original upload has not been verified/);
  assert.match(summary.evidenceStatus[1].text, /partial context/);
  assert.ok(summary.sections.some(section => section.id === "limits" && !section.deferredReading));
});

test("short summaries preserve source wording and display without bold markers", () => {
  const report = interpreted();
  const wording = "The **available words** describe a conditional decision. The full event remains unknown.";
  report.summary = prefix + wording + " " + limit;
  const meaning = buildReportSummary(report).sections.find(section => section.id === "meaning")!;
  assert.equal(meaning.deferredReading, false);
  assert.deepEqual(meaning.paragraphs, [wording]);
  assert.equal(summaryText(meaning.paragraphs[0]), "The available words describe a conditional decision. The full event remains unknown.");
});

test("evidence status follows actual fields rather than stale summary prose", () => {
  const report = interpreted();
  report.outcome = "no_verified_match";
  report.source = report.alignment = report.context = report.comparison = null;
  report.summary = prefix + "A source was definitely verified. " + limit;
  const summary = buildReportSummary(report);
  assert.match(summary.evidenceStatus[0].text, /No verified speech match/);
  assert.match(summary.evidenceStatus[1].text, /was not assessed/);
  assert.match(summary.evidenceStatus[2].text, /do not establish factual accuracy/);
});

test("partial source evidence never becomes full-event context certainty", () => {
  const report = interpreted();
  report.submitted.transcript.status = "complete";
  report.source!.transcript.status = "partial";
  report.comparison!.context_assessment = "no_material_difference_found";
  const summary = buildReportSummary(report);
  assert.match(summary.evidenceStatus[1].text, /Only partial context was examined/);
  assert.match(summary.evidenceStatus[1].text, /full event's context remains unknown/);
});

test("clip timing is explicitly estimated only for partial submitted transcripts", () => {
  const quote = { sentence_id: "s", text: "Observed words.", start_seconds: 0, end_seconds: 2 };
  assert.equal(summaryQuoteLabel(quote, true), "Clip 0:00–0:02 · timing estimated");
  assert.equal(summaryQuoteLabel(quote, false), "Transcript 0:00–0:02");
});
