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
  for (const wording of ["Matched source recording", "Submitted clip", "Transcripts", "Surrounding source context", "Technical evidence", "Post details", "Limitations and retrieval notes", "We will not raise taxes."]) assert.ok(html.includes(wording), wording);
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

function unmatchedReport(): ReportV2 {
  const value = report();
  value.outcome = "no_verified_match";
  value.source = value.alignment = value.context = value.comparison = null;
  value.limitations = [
    "Unverified source candidate: https://www.youtube.com/watch?v=FX6GQSyxA-8",
    "Unverified source candidate details: " + JSON.stringify({ url: "https://www.youtube.com/watch?v=FX6GQSyxA-8", title: "Top candidate recording", reason: "Highest ranked search lead, not verified." }),
    "Unverified source candidate: https://www.youtube.com/watch?v=JFmUO6BvwDc",
  ];
  return value;
}

test("submitted and matched recordings remain separately visible", () => {
  const value = report();
  value.limitations.push("Unverified source candidate: https://www.youtube.com/watch?v=FX6GQSyxA-8");
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /aria-label="Source video result"/);
  assert.match(primary, /aria-label="Matched source recording"/);
  assert.match(primary, /Verified speech match/);
  const sourceVideo = primary.split('aria-label="Source video result"')[1]!.split('aria-label="Spliced context video"')[0]!;
  assert.doesNotMatch(sourceVideo, /Unverified candidate:|youtube-nocookie\.com\/embed\/FX6GQSyxA-8/);
  assert.match(primary, /aria-label="Submitted excerpt"/);
  assert.ok(html.indexOf('aria-label="Source video result"') < html.indexOf('aria-label="Report verdict"'));
  assert.match(html, /<details[^>]*aria-label="Submitted clip"[^>]*><summary/);
  assert.doesNotMatch(html, /<details[^>]*aria-label="Submitted clip"[^>]* open|Compare the recordings/);
});

test("without verification the first lead is previewed with an explicit unmatched-speech note", () => {
  const value = unmatchedReport();
  const before = JSON.stringify(value);
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /Top candidate recording/);
  assert.match(primary, /Unverified lead/);
  assert.match(primary, /Search found this recording, but its speech has not been matched to your clip\./);
  assert.match(primary, /<iframe[^>]+src="https:\/\/www\.youtube-nocookie\.com\/embed\/FX6GQSyxA-8\?playsinline=1&amp;rel=0"/);
  assert.match(primary, /title="Unverified candidate: Top candidate recording"/);
  assert.match(primary, /Open candidate on YouTube/);
  assert.match(primary, /Recheck Possible related recording/);
  assert.doesNotMatch(primary, /Source analysis|Four layers|Verified speech match|autoplay|autoPlay/);
  assert.equal((primary.match(/<iframe/g) ?? []).length, 1);
  assert.match(primary, /aria-label="Submitted excerpt"/);
  assert.equal((primary.match(/<video/g) ?? []).length, 1);
  assert.equal((primary.match(/<button/g) ?? []).length, 4);
  assert.match(primary, /Select a saved candidate to recheck/);
  const otherLeads = html.split("Other source candidates")[1]!.split('aria-label="Evidence strength"')[0]!;
  assert.match(otherLeads, /JFmUO6BvwDc/);
  assert.doesNotMatch(otherLeads, /FX6GQSyxA-8/);
  assert.equal(JSON.stringify(value), before);
});

test("an unmatched report with no lead has disabled empty options and preserves input data", () => {
  const value = unmatchedReport();
  value.limitations = [];
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /A source has not been confirmed yet/);
  assert.doesNotMatch(primary, /Verified speech match|data-assessment="difference"/);
  assert.equal((primary.match(/disabled=""/g) ?? []).length, 4);
  assert.match(html, /&quot;submitted&quot;:/);
  assert.match(html, /aria-label="Submitted excerpt"/);
  assert.doesNotMatch(html, /Other source candidates/);
  assert.doesNotMatch(primary, /<iframe|Unverified candidate:|Search found this recording/);
});

test("unsafe saved candidate URLs cannot create a primary preview", () => {
  const value = unmatchedReport();
  value.limitations = ["Unverified source candidate: https://youtube.com.attacker.example/watch?v=FX6GQSyxA-8"];
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const sourceVideo = html.split('aria-label="Source video result"')[1]!.split('aria-label="Spliced context video"')[0]!;
  assert.match(sourceVideo, /No verified source video is available/);
  assert.doesNotMatch(sourceVideo, /<iframe|attacker\.example|Search found this recording/);
});

test("stale unmatched source fields cannot acquire a verified label in the main result", () => {
  const value = report();
  value.outcome = "no_verified_match";
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /A source has not been confirmed yet/);
  assert.doesNotMatch(primary, /Verified speech match|Source analysis|Four layers/);
  assert.equal((primary.match(/<video/g) ?? []).length, 1);
});

test("diagnostic mode preserves old panels in a hidden container and shows every saved field", () => {
  const value = report();
  value.summary = "<script>unsafe()</script> " + "complete diagnostic text ".repeat(300);
  value.submitted.post.author.followers_count = null;
  const model = display(value);
  const diagnostics = { job_id: value.job_id, capture_status: "recorded" as const,
    events: [{ stage: "search_candidates", status: "completed", elapsed_seconds: 1.25,
      data: { query: "verbatim words", scores: [88, 76], unknown: null } }],
    terminal_output: "search_candidates completed\nCandidate score: 88\n<script>unsafe()</script>",
  };
  const before = JSON.stringify(value);
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: model, diagnostics }));
  assert.match(html, /<div hidden=""[^>]*data-testid="preserved-report-panels"/);
  assert.match(html, /<details hidden=""[^>]*data-testid="error-diagnostics"><summary>Technical details<\/summary>/);
  assert.doesNotMatch(html, /<details[^>]*\bopen(?:=|\s|>)[^>]*data-testid="error-diagnostics"/);
  assert.match(html, /Analysis terminal|Copy output|Download \.txt/);
  assert.match(html, /Available data markers/);
  assert.match(html, /Data available for bars, labels and markers/);
  assert.match(html, /Source-match confidence/);
  assert.match(html, /Candidate score: 88/);
  assert.match(html, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>unsafe/);
  const terminal = html.split('aria-label="Complete recorded analysis output"')[1]!.split("</pre>")[0]!;
  for (const field of Object.keys(value)) assert.ok(terminal.includes(`&quot;${field}&quot;`), field);
  assert.match(terminal, /&quot;followers_count&quot;: null/);
  assert.match(terminal, /complete diagnostic text /);
  assert.equal((terminal.match(/complete diagnostic text /g) ?? []).length, 300);
  assert.equal(JSON.stringify(value), before);
});

test("stored context video appears alongside submitted and matched recordings", () => {
  const value = report();
  value.context_video = {media_id:"context_test",mime_type:"video/mp4",playback_url:"https://api.example/media/context_test",duration_seconds:79,width:640,height:360,fps:30,has_audio:true};
  value.context_selection = {start_seconds:95,end_seconds:112};
  const html = renderToStaticMarkup(createElement(ReportContent, {report:value,display:display(value)}));
  const visible = html.split('aria-label="Analysis diagnostic output"')[0]!;
  assert.match(visible, /Before clip<\/strong><span>1:35–1:41/);
  assert.match(visible, /Matched excerpt<\/strong><span>1:41–1:45/);
  assert.match(visible, /aria-label="Submitted excerpt"/);
  assert.match(visible, /aria-label="Matched source recording"/);
  assert.match(visible, /aria-label="Spliced context video"/);
  assert.equal((visible.match(/<video/g) ?? []).length, 3);
  assert.equal((html.match(/<video/g) ?? []).length, 3, "preserved UI must not mount a fourth player");
  const preserved = html.split('data-testid="preserved-report-panels"')[1]!;
  assert.doesNotMatch(preserved, /<video/);
  assert.match(visible, /data-testid="evidence-wafer"/);
  assert.match(html,/https:\/\/api.example\/media\/context_test/);
  assert.ok(html.includes(value.source!.post.post_url.replaceAll("&", "&amp;")));
});


test("low confidence suppresses even a stale generated context asset", () => {
  const value = report();
  value.alignment!.match_quality.confidence.level = "low";
  value.context_video = { ...value.source!.post.media, media_id: "context_stale", playback_url: "https://api.example/media/context_stale" };
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  const visible = html.split('aria-label="Analysis diagnostic output"')[0]!;
  assert.doesNotMatch(visible, /context_stale|aria-label="Spliced context video"[^>]*><h3/);
  assert.match(visible, /A source has not been confirmed yet/);
});

test("generated videos cannot fall back to a YouTube original", () => {
  const { default: Player } = componentRequire("../components/ReportPlayer.tsx") as typeof import("../components/ReportPlayer.tsx");
  const media = report().source!;
  media.post.media.media_id = "invalid/id";
  media.post.media.playback_url = null;
  media.post.media.mime_type = "application/x-unavailable";
  const html = renderToStaticMarkup(createElement(Player, { media, label: "Generated context", generated: true }));
  assert.doesNotMatch(html, /<iframe|Open on YouTube/);
  assert.match(html, /generated context video could not be loaded/);
});

test("the analytics view has four evidence layers and keeps source gaps separate from splice claims", () => {
  const value = report();
  const html = renderToStaticMarkup(createElement(ReportContent, { report: value, display: display(value) }));
  assert.match(html, /aria-label="Source analysis"/);
  assert.match(html, /Source-match confidence|Word overlap|Splice assessment|Detection limitations/);
  assert.match(html, /0 source gaps/);
  assert.match(html, /AI analysis may be incomplete or incorrect/);
  assert.match(html, /data-testid="evidence-wafer"/);
  assert.doesNotMatch(html, /0 splices detected|95% accurate/);
});

test("missing generated context stays hidden while submitted and matched recordings remain available", () => {
  const value = report();
  value.context_video = null;
  value.alignment!.edit_analysis.status = "not_assessed";
  const html = renderToStaticMarkup(createElement(ReportContent, {report:value,display:display(value)}));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /<section[^>]*aria-label="Spliced context video"/);
  assert.match(primary, /aria-label="Submitted excerpt"/);
  assert.match(primary, /aria-label="Matched source recording"/);
  assert.equal((primary.match(/<video/g) ?? []).length, 2);
  assert.doesNotMatch(primary, /Splice assessment|Generation time/);
  assert.match(primary, /data-testid="evidence-wafer"/);
  assert.equal((primary.match(/aria-controls="evidence-layer-/g) ?? []).length, 3, "three discs without splice assessment");
  assert.match(primary, /Excerpt aligned/);
});

test("evidence wafer stays visible and static without a loaded generated MP4", () => {
  const { default: Analytics } = componentRequire("../components/ReportAnalytics.tsx") as typeof import("../components/ReportAnalytics.tsx");
  const { reportAnalytics } = componentRequire("../lib/report-analytics.ts") as typeof import("../lib/report-analytics.ts");
  const data = reportAnalytics(report());
  for (const spliceVideoReady of [false, true, false]) {
    const html = renderToStaticMarkup(createElement(Analytics, { data, interpretationState: "unavailable", spliceVideoReady }));
    assert.match(html, /data-testid="evidence-wafer"/);
    assert.equal(html.includes('data-motion="animated"'), spliceVideoReady);
    assert.equal(html.includes('data-motion="static"'), !spliceVideoReady);
  }
  const noMatch = renderToStaticMarkup(createElement(Analytics, {data:reportAnalytics(unmatchedReport()),interpretationState:"unavailable",spliceVideoReady:true}));
  assert.doesNotMatch(noMatch, /data-testid="evidence-wafer"/);
});

test("no-match retains submitted video while hiding matched and generated video results", () => {
  const value = unmatchedReport();
  value.limitations = [];
  value.context_video = { ...report().source!.post.media, media_id: "context_stale", playback_url: "https://api.example/media/context_stale" };
  const html = renderToStaticMarkup(createElement(ReportContent, {report:value,display:display(value)}));
  const primary = html.slice(0, html.indexOf('aria-label="Analysis diagnostic output"'));
  assert.match(primary, /aria-label="Submitted video"/);
  assert.match(primary, /aria-label="Submitted excerpt"/);
  assert.equal((primary.match(/<video/g) ?? []).length, 1);
  assert.doesNotMatch(primary, /aria-label="Matched source recording"|context_stale/);
  assert.match(primary, /aria-label="Spliced context video"/);
});

test("personalized export animation requires playable video, not stored metadata", () => {
  const {default: Motion} = componentRequire("../components/AlgorithmMotion.tsx") as typeof import("../components/AlgorithmMotion.tsx");
  const {progressFromReport} = componentRequire("../lib/analysis-progress.ts") as typeof import("../lib/analysis-progress.ts");
  const value = report();
  value.context_video = {...value.source!.post.media,media_id:"context_saved"};
  const progress = progressFromReport(value);
  const pending = renderToStaticMarkup(createElement(Motion,{progress}));
  assert.doesNotMatch(pending,/Schematic, not proportional|Continuous source excerpt ready/);
  assert.match(pending,/Playback has not been confirmed/);
  const ready = renderToStaticMarkup(createElement(Motion,{progress,videoPlayable:true}));
  assert.match(ready,/Schematic, not proportional|Continuous source excerpt ready/);
  for(const stage of ["failed","no_match"] as const){
    const terminal = renderToStaticMarkup(createElement(Motion,{progress:{...progress,stage},videoPlayable:true}));
    assert.doesNotMatch(terminal,/Schematic, not proportional|Continuous source excerpt ready/);
    assert.match(terminal,/STOPPED/);
  }
});

test("three video slots precede analysis, with honest blanks for missing results",()=>{
 const value=report();value.context_video=null;
 const html=renderToStaticMarkup(createElement(ReportContent,{report:value,display:display(value)}));
 for(const name of ["Submitted video","Source video result","Spliced context video"])
   assert.ok(html.indexOf(`aria-label="${name}"`)<html.indexOf('aria-label="Source analysis"'));
 assert.match(html,/No generated context video is available/);
});

test("wafer layers are accessible buttons with explanations hidden until interaction",()=>{
 const {default:Analytics}=componentRequire("../components/ReportAnalytics.tsx") as typeof import("../components/ReportAnalytics.tsx");
 const {reportAnalytics}=componentRequire("../lib/report-analytics.ts") as typeof import("../lib/report-analytics.ts");
 const html=renderToStaticMarkup(createElement(Analytics,{data:reportAnalytics(report()),interpretationState:"unavailable"}));
 assert.match(html,/aria-label="Source-match confidence" aria-pressed="false" aria-controls="evidence-layer-0"/);
 assert.match(html,/id="evidence-layer-1" hidden=""/);
 assert.match(html,/data-motion="static"/);
});

test("source review check requires a ready recorded review and retains verification scope",()=>{
 const {default:Analytics}=componentRequire("../components/ReportAnalytics.tsx") as typeof import("../components/ReportAnalytics.tsx");
 const {reportAnalytics}=componentRequire("../lib/report-analytics.ts") as typeof import("../lib/report-analytics.ts");
 const value=report(),data=reportAnalytics(value),sentence=value.context!.windows[0].sentences[0];
 const interpretation={status:"ready",source_review:{status:"no_obvious_issue",fact_check:"No checkable factual claim in these sentences.",semantic:"The speaker is making a joke.",references:[{media_id:"media_source",sentence_id:sentence.sentence_id}]}} as unknown as import("../lib/api-types.ts").ReportInterpretation;
 const html=renderToStaticMarkup(createElement(Analytics,{data,interpretation,interpretationState:"ready",sourceEvidence:[{...sentence,media_id:"media_source"}]}));
 assert.match(html,/No clearly inaccurate data discovered/);assert.doesNotMatch(html,/Meaning:|Source evidence|external facts not independently verified/);
 assert.ok(html.indexOf('aria-label="Source excerpt review"')<html.indexOf('data-testid="evidence-wafer"'));
 const absent=renderToStaticMarkup(createElement(Analytics,{data,interpretation:{...interpretation,status:"unavailable"},interpretationState:"unavailable"}));
 assert.doesNotMatch(absent,/No clearly inaccurate data discovered/);
 const missing=renderToStaticMarkup(createElement(Analytics,{data,interpretation,interpretationState:"ready",sourceEvidence:[]}));
 assert.match(missing,/No clearly inaccurate data discovered/);
 const stale=renderToStaticMarkup(createElement(Analytics,{data,interpretation:{...interpretation,source_review:{...interpretation.source_review!,references:[...interpretation.source_review!.references,{media_id:"media_source",sentence_id:"missing"}]}},interpretationState:"ready",sourceEvidence:[{...sentence,media_id:"media_source"}]}));
 assert.match(stale,/No clearly inaccurate data discovered/);
});


test("hovered evidence layer expands upper gaps while retaining the lower stack",()=>{
 const {evidenceLayerPosition:position}=componentRequire("../components/ReportAnalytics.tsx") as typeof import("../components/ReportAnalytics.tsx");
 assert.deepEqual([0,1,2,3].map(i=>position(i,null)),[0,24,48,72]);
 assert.ok(position(1,1)-position(0,1)>24);
 assert.equal(position(2,1),168);assert.equal(position(3,1),192);
 assert.ok(position(2,2)-position(1,2)>18);
});


test("source review resolves only recorded citations to actual sentence quotes",()=>{
 const {default:Analytics}=componentRequire("../components/ReportAnalytics.tsx") as typeof import("../components/ReportAnalytics.tsx");
 const {reportAnalytics}=componentRequire("../lib/report-analytics.ts") as typeof import("../lib/report-analytics.ts");
 const value=report(),sentence=value.context!.windows[0].sentences[0];
 const interpretation={status:"ready",source_review:{status:"potential_issue",fact_check:"Claim requires checking.",semantic:"A qualification matters.",references:[{media_id:"media_source",sentence_id:sentence.sentence_id},{media_id:"wrong_media",sentence_id:sentence.sentence_id},{media_id:"media_source",sentence_id:"missing"}]}} as unknown as import("../lib/api-types.ts").ReportInterpretation;
 const html=renderToStaticMarkup(createElement(Analytics,{data:reportAnalytics(value),interpretation,interpretationState:"ready",sourceEvidence:[{...sentence,media_id:"media_source"}]}));
 assert.match(html,/Source evidence \(1\)/);assert.match(html,/We will not raise taxes/);assert.match(html,/1:41/);assert.doesNotMatch(html,/wrong_media|missing/);
});

test("personalized loading reuses the homepage fragment visual with real transcript words",()=>{
 const {default:Motion}=componentRequire("../components/AlgorithmMotion.tsx") as typeof import("../components/AlgorithmMotion.tsx");
 const {parseProgress}=componentRequire("../lib/analysis-progress.ts") as typeof import("../lib/analysis-progress.ts");
 const progress=parseProgress({job_id:"job",stage:"transcript",excerpt:"Actual recorded speech words"},"job");
 const html=renderToStaticMarkup(createElement(Motion,{progress}));
 assert.match(html,/data-testid="personalized-fragments"/);assert.match(html,/stage-fragment/);assert.match(html,/Actual recorded speech words/);assert.doesNotMatch(html,/Continuous source excerpt ready/);
});

test("report display replaces repeated media identifiers with clip roles",()=>{
 const {clipDisplayText}=componentRequire("../lib/report-summary.ts") as typeof import("../lib/report-summary.ts");
 assert.equal(clipDisplayText("media_abc follows media_def; media_abc.","media_abc","media_def"),"user clip follows source clip; user clip.");
 assert.equal(clipDisplayText(null,"media_abc"),"");
});
