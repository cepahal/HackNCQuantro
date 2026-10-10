import type { AssessmentBlock, Author, ConfidenceLevel, ConfidenceObject, DisplayModel, ReportV2 } from "./api-types";

// Wording mirrors backend/services/reporting/display_report.py.
export const SOURCE_MATCH_LABEL = "Source-match confidence";
export const CLIP_COVERAGE_LABEL = "Submitted clip aligned";
export const CONTEXT_ASSESSMENT_LABEL = "Context-assessment confidence";
export const CONFIDENCE_NOTE = "Confidence levels describe the available evidence; they are not calibrated probabilities.";
export const NO_SOURCE_MESSAGE = "No verified source found";
export const CONTEXT_UNAVAILABLE_MESSAGE = "Context assessment unavailable";
export const NOT_ASSESSED = "Not assessed";
export const UNAVAILABLE = "Unavailable";

const LEVEL_LABELS: Record<ConfidenceLevel, string> = {
  high: "High", moderate: "Moderate", low: "Low", not_assessed: NOT_ASSESSED,
};

export type ConfidenceDisplay = Pick<
  DisplayModel,
  "source_match" | "clip_coverage" | "context_assessment" | "confidence_note" | "no_source_message" | "context_unavailable_message"
>;

/** Client-only: the poster card shows identity fields the backend PosterInfo block omits. */
export interface PosterCard {
  handle: string;
  followers_count: string;
  verified: string;
}

export function levelLabel(level: ConfidenceLevel): string {
  return LEVEL_LABELS[level];
}

/** Null means unknown, so it is never shown as 0. */
export function displayCount(value: number | null, missing: string): string {
  return value === null ? missing : value.toLocaleString("en-US");
}

function assessmentBlock(label: string, confidence: ConfidenceObject | null): AssessmentBlock {
  return confidence === null
    ? { label, value: NOT_ASSESSED, basis: [], limitations: [] }
    : { label, value: levelLabel(confidence.level), basis: confidence.basis, limitations: confidence.limitations };
}

export function confidenceDisplay(report: ReportV2): ConfidenceDisplay {
  const matched = report.outcome === "matched" && report.alignment !== null;
  const quality = matched ? report.alignment!.match_quality : null;
  const context = assessmentBlock(CONTEXT_ASSESSMENT_LABEL, report.comparison?.assessment_confidence ?? null);
  return {
    source_match: quality ? assessmentBlock(SOURCE_MATCH_LABEL, quality.confidence) : null,
    clip_coverage: quality ? {
      label: CLIP_COVERAGE_LABEL,
      value: quality.clip_coverage_ratio === null ? NOT_ASSESSED : `${Math.round(quality.clip_coverage_ratio * 100)}%`,
      description: quality.matched_clip_seconds === null
        ? NOT_ASSESSED : `${quality.matched_clip_seconds} seconds aligned to the selected source.`,
    } : null,
    context_assessment: context,
    confidence_note: CONFIDENCE_NOTE,
    no_source_message: matched ? null : NO_SOURCE_MESSAGE,
    context_unavailable_message: context.value === NOT_ASSESSED ? CONTEXT_UNAVAILABLE_MESSAGE : null,
  };
}

export function posterCard(author: Author): PosterCard {
  return {
    handle: author.handle ?? UNAVAILABLE,
    followers_count: displayCount(author.followers_count, UNAVAILABLE),
    verified: author.verified === null ? UNAVAILABLE : author.verified ? "Verified" : "Not verified",
  };
}

export function timeRange(start: number, end: number): string {
  return `${start.toFixed(1)}s–${end.toFixed(1)}s`;
}
