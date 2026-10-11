import assert from "node:assert/strict";
import test from "node:test";
import { sourceLeads } from "../lib/source-leads.ts";
import { reportFixture } from "./fixtures/report_v2.ts";
import type { ReportV2 } from "../lib/api-types.ts";

const link = "https://www.youtube.com/watch?v=FX6GQSyxA-8";
test("discovered lead preserves title and uncertainty without inventing a matched source", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.outcome = "no_verified_match";
  report.source = null;
  report.alignment = null;
  report.limitations = [
    `Unverified source candidate: ${link}`,
    "Unverified source candidate details: " + JSON.stringify({ url: link, title: "Trump orders 30 milkshakes", reason: "Discovery metadata only; not verified." }),
  ];
  const before = JSON.stringify(report);
  assert.equal(sourceLeads(report)[0].title, "Trump orders 30 milkshakes");
  assert.match(sourceLeads(report)[0].reason, /not verified/);
  assert.match(sourceLeads(report)[0].preview, /^https:\/\/www.youtube-nocookie.com\/embed\//);
  assert.equal(JSON.stringify(report), before);
});

test("old reports expose safe standalone links with a clear unverified fallback", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.limitations = [`Unverified source candidate: ${link}`];
  assert.equal(sourceLeads(report)[0].url, link);
  assert.match(sourceLeads(report)[0].reason, /not been verified/);
});

test("malformed annotations, credentials, lookalike hosts and internal URLs never create leads", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.limitations = ["Unverified source candidate details: {", ...[
    "https://youtube.com.evil.test/watch?v=FX6GQSyxA-8", "https://token@youtube.com/watch?v=FX6GQSyxA-8",
    "http://localhost:8000/media/test", "javascript:alert(1)", "https://www.youtube.com/watch?v=short",
  ].map(url => `Unverified source candidate: ${url}`)];
  assert.deepEqual(sourceLeads(report), []);
});

test("equivalent URLs deduplicate and submitted or verified recording is excluded", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.submitted.post.post_url = link;
  report.limitations = [`Unverified source candidate: ${link}`, "Unverified source candidate: https://youtu.be/FX6GQSyxA-8"];
  assert.deepEqual(sourceLeads(report), []);
  report.submitted.post.post_url = "https://www.youtube.com/watch?v=-d6ZbbUAMNo";
  assert.equal(sourceLeads(report).length, 1);
  report.source!.post.post_url = link;
  assert.deepEqual(sourceLeads(report), []);
});

test("at most five distinct safe leads are exposed and metadata remains plain bounded text", () => {
  const report = structuredClone(reportFixture) as ReportV2;
  report.limitations = ["FX6GQSyxA-8", "JFmUO6BvwDc", "zRfLAeS1xg4", "TCm9788Tb5g", "www2SH5c68o", "zXIxDoCRc84"].map(id => `Unverified source candidate: https://www.youtube.com/watch?v=${id}`);
  report.limitations.push("Unverified source candidate details: " + JSON.stringify({ url: link, title: "**<script>x</script>**\u2014" + "x".repeat(300), reason: null }));
  assert.equal(sourceLeads(report).length, 5);
  assert.equal(sourceLeads(report)[0].title.length, 200);
  assert.doesNotMatch(sourceLeads(report)[0].title, /\*\*|\u2014/);
  assert.ok(sourceLeads(report)[0].title.includes("<script>"));
});
