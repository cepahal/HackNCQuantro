import type { ReportV2, TranscriptSentence } from "./api-types.ts";
import { reportSummary } from "./display.ts";
import { time } from "./format.ts";

const MEANING_PREFIX = "A tentative reading of the submitted clip is: ";
const LEGACY_MEANING_PREFIX = "Assuming this is political speech, a tentative reading of the submitted transcript is: ";
const EVIDENCE_PREFIX = "Transcript evidence: ";
const REFERENCES_PREFIX = "Possible references (unverified hypotheses only): ";
const PIPELINE_PREFIX = "Existing pipeline result: ";
const LIMIT = "Factual accuracy and deceptive intent are not established by this partial transcript.";
const CAPTION_RELATIONS = [
  "No submitted caption was provided for interpretation.",
  "The caption's relationship to this transcript is uncertain.",
  "The caption appears to discuss the same subject; its truth and surrounding context remain unestablished.",
  "A relationship between the caption and this transcript was not established.",
];

export type SummaryQuote = Pick<TranscriptSentence, "text" | "start_seconds" | "end_seconds" | "sentence_id">;
export type SummarySection = {
  id: string;
  title: string;
  paragraphs: string[];
  quotes: SummaryQuote[];
  note?: string;
  deferredReading?: boolean;
};
export type ReadableSummary = {
  politicalAssumption: boolean;
  partialTranscript: boolean;
  evidenceStatus: Array<{ label: string; text: string }>;
  metadataClues: Array<{ label: string; text: string }>;
  sections: SummarySection[];
};

/** Normalize typography only. Callers render the result as escaped React text. */
export function summaryText(text: string): string {
  return text.replace(/\*\*/g, "").replace(/\s*\u2014\s*/g, ", ");
}

export function summaryQuoteLabel(quote: SummaryQuote, partial: boolean): string {
  const range = `${time(quote.start_seconds)}–${time(quote.end_seconds)}`;
  return partial ? `Clip ${range} · timing estimated` : `Transcript ${range}`;
}

/** Extract an existing first sentence for long saved reports without inventing a reading. */
export function summaryTakeaways(reading: SummarySection | undefined): string[] {
  if (!reading) return [];
  if (!reading.deferredReading) return reading.paragraphs;
  const first = reading.paragraphs[0] ?? "";
  const sentence = first.match(/^[\s\S]*?[.!?](?=\s|$)/)?.[0];
  return [sentence ?? first].filter(Boolean);
}

function evidenceStatus(report: ReportV2): ReadableSummary["evidenceStatus"] {
  const verified = report.outcome === "matched" && report.source !== null
    && report.alignment?.status === "verified" && report.alignment.matches.length > 0
    && report.alignment.source_media_id === report.source.post.media.media_id
    && report.alignment.clip_media_id === report.submitted.post.media.media_id;
  const partial = report.submitted.transcript.status === "partial"
    || report.source?.transcript.status === "partial"
    || report.context?.windows.some(window => window.coverage !== "complete");
  let context = "Surrounding source context was not assessed.";
  if (report.source && report.context?.windows.some(window => window.sentences.length > 0)) {
    if (partial) context = "Only partial context was examined. The full event's context remains unknown.";
    else if (report.comparison?.context_assessment === "uncertain") context = "The meaning of the supplied source context remains uncertain.";
    else if (report.comparison?.context_assessment === "material_context_missing") context = "The supplied context contains a reported difference; see the cited findings.";
    else if (report.comparison?.context_assessment === "no_material_difference_found") context = "No material difference was reported in the supplied source context.";
  }
  return [
    { label: "Speech match", text: verified
      ? "Matching speech was found in another recording. The original upload has not been verified."
      : "No verified speech match was found. A source remains unconfirmed." },
    { label: "Source context", text: context },
    { label: "What this does not prove", text: "Matching words do not establish factual accuracy, intent, or recording authenticity." },
  ];
}

function paragraphs(text: string): string[] {
  return text.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean);
}

function section(id: string, title: string, text: string): SummarySection {
  return { id, title, paragraphs: paragraphs(text), quotes: [],
    deferredReading: (id === "meaning" || id === "summary") && text.trim().split(/\s+/).length > 70 };
}

/** Only the backend's exact, framed summary format is split. Other prose stays intact. */
export function conciseSummary(report: ReportV2): { takeaways: string; meaning: string; reliability: string } | null {
  const text = report.summary?.trim() || report.comparison?.summary.trim() || "";
  const match = text.match(/^Takeaways:\s*([\s\S]+?)\n\s*\nWhat this means:\s*([\s\S]+?)\n\s*\nReliability:\s*([\s\S]+)$/);
  return match ? { takeaways: match[1].trim(), meaning: match[2].trim(), reliability: match[3].trim() } : null;
}

export function buildReportSummary(report: ReportV2): ReadableSummary {
  const text = reportSummary(report);
  const partialTranscript = report.submitted.transcript.status === "partial"
    || Boolean(report.source?.transcript.status === "partial");
  const metadataClues = [
    { label: "Post title", text: report.submitted.post.title },
    { label: "Post caption", text: report.submitted.post.caption },
  ].filter((item): item is { label: string; text: string } => Boolean(item.text?.trim()));
  const fallback: ReadableSummary = {
    politicalAssumption: false, partialTranscript, evidenceStatus: evidenceStatus(report), metadataClues,
    sections: [section("summary", "Summary", text)],
  };
  const meaningPrefix = [MEANING_PREFIX, LEGACY_MEANING_PREFIX].find(prefix => text.startsWith(prefix));
  const limitAt = text.lastIndexOf(` ${LIMIT}`);
  if (!meaningPrefix || limitAt < meaningPrefix.length) return fallback;

  let body = text.slice(meaningPrefix.length, limitAt).trim();
  const tail = text.slice(limitAt + LIMIT.length + 1).trim();
  const pipeline = tail.startsWith(PIPELINE_PREFIX) ? tail.slice(PIPELINE_PREFIX.length) : "";
  const extraLimit = tail && !pipeline ? tail : "";
  let caption = "";
  for (const relation of CAPTION_RELATIONS) {
    if (body.endsWith(` ${relation}`)) {
      caption = relation;
      body = body.slice(0, -(relation.length + 1)).trim();
      break;
    }
  }
  let references = "";
  const referencesAt = body.lastIndexOf(` ${REFERENCES_PREFIX}`);
  if (referencesAt >= 0) {
    references = body.slice(referencesAt + REFERENCES_PREFIX.length + 1).trim();
    body = body.slice(0, referencesAt).trim();
  }
  let evidence = "";
  const evidenceAt = body.lastIndexOf(` ${EVIDENCE_PREFIX}`);
  if (evidenceAt >= 0) {
    evidence = body.slice(evidenceAt + EVIDENCE_PREFIX.length + 1).trim();
    body = body.slice(0, evidenceAt).trim();
  }
  if (!body) return fallback;

  const meaning = section("meaning", "Meaning", body);
  const politicalAssumption = meaningPrefix === LEGACY_MEANING_PREFIX;
  if (politicalAssumption) meaning.note = "This saved interpretation assumed political speech; that assumption has not been verified.";
  const sections = [meaning];
  if (evidence) {
    const observed = section("evidence", "Transcript evidence", "");
    const byText = new Map<string, TranscriptSentence[]>();
    for (const sentence of report.submitted.transcript.sentences) {
      byText.set(sentence.text, [...(byText.get(sentence.text) ?? []), sentence]);
    }
    let remainder = evidence;
    for (const [wording, matches] of byText) {
      const quoted = `“${wording}”`;
      if (!wording.trim() || matches.length !== 1 || !remainder.includes(quoted)) continue;
      observed.quotes.push(matches[0]);
      remainder = remainder.replace(quoted, "");
    }
    observed.quotes.sort((a, b) => evidence.indexOf(`“${a.text}”`) - evidence.indexOf(`“${b.text}”`));
    remainder = remainder.replace(/^[\s/]+|[\s/]+$/g, "").trim();
    if (remainder) {
      observed.paragraphs = [`Unlinked wording from the summary: ${remainder}`];
      observed.note = "This wording could not be uniquely linked to a complete submitted transcript sentence.";
    } else {
      observed.note = "Complete supplied transcript sentences. Transcript wording and timing may still contain errors.";
    }
    sections.push(observed);
  }
  if (references) {
    const hypotheses = section("references", "Possible references · Unverified", references);
    hypotheses.note = "Suggested interpretations, not established events or verified facts.";
    sections.push(hypotheses);
  }
  if (caption) sections.push(section("caption", "Caption relation", caption));
  if (pipeline) {
    const finding = section("finding", "What we found", pipeline.replace(" Partial transcript context: ", "\n\nPartial transcript context: "));
    if (report.source) finding.note = "A matching recording does not establish the earliest original upload.";
    sections.push(finding);
  }
  const limits = section("limits", "Limits", [LIMIT, extraLimit].filter(Boolean).join("\n\n"));
  if (partialTranscript) limits.note = "Only partial transcript evidence was available; unobserved speech and full context remain unknown.";
  sections.push(limits);
  return { politicalAssumption, partialTranscript, evidenceStatus: evidenceStatus(report), metadataClues, sections };
}
