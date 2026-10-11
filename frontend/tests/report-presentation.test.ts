import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { DisplayModel, ReportV2 } from "../lib/api-types.ts";
import { confidenceDisplay } from "../lib/display.ts";
import { reportFixture } from "./fixtures/report_v2.ts";

// Node's native TypeScript loader does not accept JSX. Use the existing compiler
// for component-level checks and stub CSS only inside this isolated test worker.
const componentRequire = createRequire(import.meta.url);
const typescript = componentRequire("typescript") as typeof import("typescript");
for (const extension of [".tsx", ".ts"]) {
  componentRequire.extensions[extension] = (loaded, filename) => {
    const output = typescript.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { jsx: typescript.JsxEmit.ReactJSX, module: typescript.ModuleKind.CommonJS, esModuleInterop: true, target: typescript.ScriptTarget.ES2022 },
    });
    (loaded as unknown as { _compile: (text: string, path: string) => void })._compile(output.outputText, filename);
  };
}
componentRequire.extensions[".css"] = loaded => { loaded.exports = new Proxy({}, { get: (_target, key) => key }); };
const { default: ReportSummary, reportAssessment, ReportVerdict, conciseSummary } = componentRequire("../components/ReportSummary.tsx") as typeof import("../components/ReportSummary.tsx");
const { ReportContent } = componentRequire("../components/ReportPage.tsx") as typeof import("../components/ReportPage.tsx");

function report(): ReportV2 {
  const value = structuredClone(reportFixture);
  value.comparison.findings[0].evidence.push({ media_id: value.submitted.post.media.media_id,
    sentence_ids: ["submitted_s1"], start_seconds: 1, end_seconds: 4.5, quote: "We will not raise taxes." });
  return value;
}
function display(value: ReportV2): DisplayModel {
  return { ...confidenceDisplay(value), submitted_panel: { post: {}, transcript: null, media_playback_url: null },
    source_panel: null, poster_info: { submitted: { followers_count: "Unavailable", following_count: "Unavailable", posts_count: "Unavailable" }, source: null },
    matches: [], edit_analysis: null, findings: [], limitations: [] };
}

test("verified source and cited contradiction render green match and red findings", () => {
  const value = report();
  assert.deepEqual(reportAssessment(value).items.map(item => item.tone), ["supported", "difference", "difference"]);
  const html = renderToStaticMarkup(createElement(ReportVerdict, { report: value }));
  assert.match(html, /data-assessment="supported"/);
  assert.match(html, /data-assessment="difference"/);
  assert.match(html, /The post claim conflicts with source context/);
});

test("no-match remains neutral even with stale contradictory labels", () => {
  const value = report();
  value.outcome = "no_verified_match";
  assert.equal(reportAssessment(value).tone, "neutral");
  assert.ok(reportAssessment(value).items.every(item => item.tone === "neutral"));
});

test("mismatched media IDs never produce a successful source check", () => {
  const value = report();
  value.alignment!.source_media_id = "unrelated_recording";
  assert.ok(reportAssessment(value).items.every(item => item.tone === "neutral"));
});

test("missing citations, altered quotes, and not-assessed confidence do not produce red verdicts", () => {
  for (const change of [
    (value: ReportV2) => { value.comparison!.findings = []; },
    (value: ReportV2) => { value.comparison!.findings[0].evidence[0].quote = "An invented quote."; },
    (value: ReportV2) => { value.comparison!.assessment_confidence.level = "not_assessed"; },
    (value: ReportV2) => { value.context = null; },
  ]) {
    const value = report(); change(value);
    assert.equal(reportAssessment(value).tone, "neutral");
    assert.deepEqual(reportAssessment(value).items.map(item => item.tone), ["supported", "neutral", "neutral"]);
  }
});

test("cited source alignment and a supported caption allow positive checks", () => {
  const value = report();
  value.comparison!.context_assessment = "no_material_difference_found";
  value.comparison!.caption_assessment = "supported_by_context";
  assert.equal(reportAssessment(value).tone, "supported");
  assert.ok(reportAssessment(value).items.every(item => item.tone === "supported"));
});

test("uncertain and not-assessed labels stay neutral", () => {
  for (const assessment of ["uncertain", "not_assessed"] as const) {
    const value = report();
    value.comparison!.context_assessment = assessment;
    value.comparison!.caption_assessment = assessment;
    assert.equal(reportAssessment(value).tone, "neutral");
    assert.deepEqual(reportAssessment(value).items.slice(1).map(item => item.tone), ["neutral", "neutral"]);
  }
});

test("a missing caption stays neutral even when a saved label claims contradiction", () => {
  const value = report();
  value.submitted.post.caption = null;
  assert.equal(reportAssessment(value).items[2].tone, "neutral");
  assert.equal(reportAssessment(value).items[2].value, "No claim supplied");
});

test("material context missing requires valid evidence from both recordings", () => {
  const value = report();
  value.comparison!.findings[0].evidence = value.comparison!.findings[0].evidence.filter(item => item.media_id !== value.submitted.post.media.media_id);
  assert.equal(reportAssessment(value).items[1].tone, "neutral");
  assert.equal(reportAssessment(value).items[2].tone, "difference");
});

test("a source quote can span several cited sentences without losing its assessment", () => {
  const value = report();
  value.comparison!.findings[0].evidence[0].sentence_ids = ["source_s1", "source_s2"];
  value.comparison!.findings[0].evidence[0].quote = "We will not raise taxes. Unless we must.";
  assert.equal(reportAssessment(value).items[2].tone, "difference");
});

test("new Groq section labels render in their intended sections with safe text", () => {
  const value = report();
  value.summary = "Takeaways: A **conditional** policy statement.\n\nWhat this means: The qualifier limits the claim.\n\nReliability: <script>alert(1)</script> Source evidence is partial.";
  assert.deepEqual(conciseSummary(value), { takeaways: "A **conditional** policy statement.", meaning: "The qualifier limits the claim.", reliability: "<script>alert(1)</script> Source evidence is partial." });
  const html = renderToStaticMarkup(createElement(ReportSummary, { report: value }));
  assert.match(html, /A conditional policy statement/);
  assert.match(html, /aria-label="What this means"/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /Takeaways:|Reliability:|<script>|\*\*/);
});

test("ordinary saved summaries are preserved by the existing parser", () => {
  const value = report();
  value.summary = "Saved summary with ordinary wording.";
  assert.equal(conciseSummary(value), null);
  assert.match(renderToStaticMarkup(createElement(ReportSummary, { report: value })), /Saved summary with ordinary wording/);
});

test("recordings, transcripts, context, and technical evidence stay reachable in disclosures", () => {
  const value = report();
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  for (const wording of ["Compare the recordings", "Transcripts", "Surrounding source context", "Technical evidence", "Post details", "Limitations and retrieval notes", "We will not raise taxes."]) assert.ok(html.includes(wording), wording);
  assert.match(html, /<details[^>]*><summary/);
  assert.doesNotMatch(html, /<details[^>]+ open/);
});

test("unknown numeric coverage and edit counts stay unavailable instead of zero", () => {
  const value = report();
  value.alignment!.match_quality.clip_coverage_ratio = null;
  value.alignment!.edit_analysis.counts.confirmed_added_segments = null;
  const before = JSON.stringify(value);
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  assert.match(html, /Excerpt aligned<\/dt><dd>Unavailable<\/dd>/);
  assert.match(html, /confirmed added segments<\/dt><dd>Unavailable<\/dd>/);
  assert.doesNotMatch(html, /100% accurate|misinformation|deceptive intent confirmed/i);
  assert.equal(JSON.stringify(value), before);
});
