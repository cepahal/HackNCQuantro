import type {
  AssessmentBlock, Author, CaptionAssessment, ConfidenceLevel, ConfidenceObject, ContextAssessment, DisplayModel, ReportV2,
} from "./api-types";

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

export function secondsLabel(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? NOT_ASSESSED : `${Math.round(value * 10) / 10}s`;
}

const CONTEXT_SENTENCES: Record<ContextAssessment, string> = {
  material_context_missing: "The surrounding source context suggests material context is missing from the submitted clip.",
  no_material_difference_found: "No material difference was found between the clip and its surrounding source context.",
  uncertain: "It is uncertain whether the surrounding source context changes the clip's meaning.",
  not_assessed: "The surrounding context was not assessed.",
};

const CAPTION_SENTENCES: Record<CaptionAssessment, string> = {
  supported_by_context: "The caption appears supported by the source context.",
  contradicted_by_context: "The caption appears contradicted by the source context.",
  uncertain: "It is uncertain whether the source context supports the caption.",
  not_provided: "The post had no caption to assess.",
  not_assessed: "The caption was not assessed.",
};

function sentence(text: string): string {
  const trimmed = text.trim();
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

/** Deterministic plain-language paragraph built only from report fields; used when the backend wrote no summary. */
export function generatedSummary(report: ReportV2): string {
  const parts: string[] = [];
  const quality = report.outcome === "matched" ? report.alignment?.match_quality ?? null : null;
  if (!quality) {
    parts.push(`${NO_SOURCE_MESSAGE} for this post. That does not establish that the clip is accurate or unedited.`);
  } else {
    const level = quality.confidence.level;
    parts.push(level === "not_assessed"
      ? "A source recording was matched; source-match confidence was not assessed."
      : `A source recording was matched with ${levelLabel(level).toLowerCase()} source-match confidence.`);
    if (quality.clip_coverage_ratio !== null) {
      const aligned = quality.matched_clip_seconds === null ? "" : ` (${Math.round(quality.matched_clip_seconds * 10) / 10} seconds)`;
      parts.push(`${Math.round(quality.clip_coverage_ratio * 100)}% of the submitted clip${aligned} aligned to the selected source.`);
    }
  }
  const comparison = report.comparison;
  if (comparison) {
    parts.push(CONTEXT_SENTENCES[comparison.context_assessment], CAPTION_SENTENCES[comparison.caption_assessment]);
    if (comparison.assessment_confidence.level !== "not_assessed") {
      parts.push(`Context-assessment confidence is ${levelLabel(comparison.assessment_confidence.level).toLowerCase()}.`);
    }
    const finding = comparison.findings[0]?.explanation;
    if (finding?.trim()) parts.push(`Key finding: ${sentence(finding)}`);
  }
  const count = report.limitations.length;
  if (count) parts.push(`The report lists ${count} ${count === 1 ? "limitation" : "limitations"}.`);
  return parts.join(" ");
}

export function reportSummary(report: ReportV2): string {
  return report.summary?.trim() || report.comparison?.summary.trim() || generatedSummary(report);
}

/** Shape the report page's error card renders; `code`/`stage` fall back when the failure was not an API envelope. */
export interface ErrorDisplay {
  error: { code: string; stage: string; message: string; retryable: boolean };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function errorDisplay(reason: unknown, fallback = "The request could not be completed."): ErrorDisplay {
  if (reason instanceof Error) {
    const api = reason as Error & { status?: unknown; code?: unknown; stage?: unknown; retryable?: unknown };
    return { error: {
      code: typeof api.code === "string" ? api.code : typeof api.status === "number" ? `HTTP ${api.status}` : "REQUEST_FAILED",
      stage: typeof api.stage === "string" ? api.stage : "unknown",
      message: reason.message || fallback,
      retryable: typeof api.retryable === "boolean" ? api.retryable : true,
    } };
  }
  const body = isObject(reason) && isObject(reason.error) ? reason.error : {};
  return { error: {
    code: typeof body.code === "string" ? body.code : "REQUEST_FAILED",
    stage: typeof body.stage === "string" ? body.stage : "unknown",
    message: typeof body.message === "string" ? body.message : fallback,
    retryable: typeof body.retryable === "boolean" ? body.retryable : true,
  } };
}
