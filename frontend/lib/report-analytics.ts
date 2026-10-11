import type { ReportV2 } from "./api-types.ts";
import type { ReportDiagnostics } from "./report-diagnostics.ts";
import { buildReportSummary, clipDisplayText, conciseSummary, summaryText } from "./report-summary.ts";

export interface AnalyticsMetrics {
  generationSeconds: number | null;
  summary: string;
  contextWindows: Array<{ start: number; end: number }>;
}
export interface WaferData {
  confidence: string;
  clipCoverage: number | null;
  wordMatches: { matched: number; total: number } | null;
  sourceGaps: number | null;
  limitations: string[];
}
export interface AnalyticsData {
  isVideoDetected: boolean;
  contextText: string;
  metrics: AnalyticsMetrics;
  wafer: WaferData;
}

const tokens = (text: string) => text.toLocaleLowerCase("en").match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)*/gu) ?? [];

/** Recorded metrics only. Token overlap is a count, never confidence or accuracy. */
export function reportAnalytics(report: ReportV2, diagnostics?: ReportDiagnostics | null): AnalyticsData {
  const alignment = report.alignment;
  const isVideoDetected = report.outcome === "matched" && report.source !== null
    && alignment?.status === "verified" && alignment.matches.length > 0
    && ["high", "moderate"].includes(alignment.match_quality.confidence.level)
    && alignment.clip_media_id === report.submitted.post.media.media_id
    && alignment.source_media_id === report.source.post.media.media_id;
  const reading = buildReportSummary(report);
  const concise = conciseSummary(report);
  const meaning = reading.sections.find(section => section.id === "meaning" || section.id === "summary");
  const contextText = summaryText(report.comparison?.summary || meaning?.paragraphs[0]
    || "The source contains matching speech. Review its surrounding words before drawing conclusions about the excerpt.");
  const timing = diagnostics?.events.findLast(event => event.stage === "run_analysis"
    && event.status === "completed" && event.timing_kind === "job_elapsed");
  let wordMatches: WaferData["wordMatches"] = null;
  if (isVideoDetected && report.source && alignment) {
    const refs = new Set(alignment.matches.flatMap(match => match.evidence.flatMap(item => item.source_refs)));
    const sourceWords = tokens(report.source.transcript.sentences.filter(sentence => refs.has(sentence.sentence_id)
      || alignment.matches.some(match => sentence.start_seconds < match.source_end_seconds && sentence.end_seconds > match.source_start_seconds))
      .map(sentence => sentence.text).join(" "));
    const clipWords = tokens(report.submitted.transcript.sentences.map(sentence => sentence.text).join(" "));
    if (clipWords.length && sourceWords.length) {
      const counts = new Map<string, number>();
      for (const word of sourceWords) counts.set(word, (counts.get(word) ?? 0) + 1);
      let matched = 0;
      for (const word of clipWords) {
        const count = counts.get(word) ?? 0;
        if (count) { matched++; counts.set(word, count - 1); }
      }
      wordMatches = { matched, total: clipWords.length };
    }
  }
  const windows = isVideoDetected && report.context && report.context.source_media_id === report.source?.post.media.media_id
    ? report.context.windows.filter(window => window.coverage !== "unavailable").map(window => ({
      start: window.requested_start_seconds, end: window.requested_end_seconds,
    })) : [];
  return {
    isVideoDetected,
    contextText: clipDisplayText(concise?.meaning || contextText, report.submitted.post.media.media_id, report.source?.post.media.media_id),
    metrics: { generationSeconds: timing && Number.isFinite(timing.elapsed_seconds) ? timing.elapsed_seconds : null,
      summary: clipDisplayText(concise?.takeaways || meaning?.paragraphs[0] || report.comparison?.summary || "No written summary was recorded.", report.submitted.post.media.media_id, report.source?.post.media.media_id), contextWindows: windows },
    wafer: {
      confidence: isVideoDetected ? ({ high: "High", moderate: "Moderate", low: "Low", not_assessed: "Not assessed" }[alignment!.match_quality.confidence.level]) : "Not assessed",
      clipCoverage: isVideoDetected && Number.isFinite(alignment?.match_quality.clip_coverage_ratio)
        ? Math.max(0, Math.min(1, alignment!.match_quality.clip_coverage_ratio!)) : null,
      wordMatches,
      sourceGaps: isVideoDetected && alignment?.edit_analysis.status === "assessed" ? alignment.edit_analysis.counts.source_gaps : null,
      limitations: [...new Set([...report.limitations, ...(alignment?.limitations ?? []),
        ...(alignment?.match_quality.confidence.limitations ?? []), ...(report.context?.limitations ?? []),
        ...(report.comparison?.limitations ?? []), ...report.submitted.post.retrieval.warnings,
        ...(report.source?.post.retrieval.warnings ?? [])])].filter(note => !note.startsWith("Unverified source candidate")),
    },
  };
}
