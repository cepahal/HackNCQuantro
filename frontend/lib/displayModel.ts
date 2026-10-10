// Mirrors DisplayModel in backend/services/reporting/display_report.py.

export type ConfidenceLabel = "High" | "Moderate" | "Low" | "Not assessed";

export interface AssessmentBlock {
  label: string;
  value: ConfidenceLabel;
  basis: string[];
  limitations: string[];
}

export interface ClipCoverageBlock {
  label: string;
  value: string;
  description: string;
}

export interface MediaPanel {
  post: Record<string, unknown>;
  transcript: Record<string, unknown> | null;
  media_playback_url: string | null;
}

export interface PosterInfo {
  followers_count: number | "Unavailable";
  following_count: number | "Unavailable";
  posts_count: number | "Unavailable";
}

export interface PosterInfoBlock {
  submitted: PosterInfo;
  source: PosterInfo | null;
}

export interface DisplayModel {
  source_match: AssessmentBlock | null;
  clip_coverage: ClipCoverageBlock | null;
  context_assessment: AssessmentBlock | null;
  confidence_note: string;
  no_source_message: string | null;
  context_unavailable_message: string | null;
  submitted_panel: MediaPanel;
  source_panel: MediaPanel | null;
  poster_info: PosterInfoBlock;
  matches: Record<string, unknown>[];
  edit_analysis: Record<string, unknown> | null;
  findings: Record<string, unknown>[];
  limitations: string[];
}
