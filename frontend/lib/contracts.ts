// Generated from backend/services/shared/types.py; do not edit by hand.
export type Alignment = {
  status: "verified" | "no_verified_match" | "not_assessed";
  clip_media_id: string;
  source_media_id: string;
  matches: Array<Match>;
  match_quality: MatchQuality;
  edit_analysis: EditAnalysis;
  limitations: Array<string>;
};

export type AnalysisResult = {
  job_id: string;
  report_id: string;
  report_url: string;
  outcome: Outcome;
  delivery_status: "accepted" | "failed" | "skipped";
  delivery_warning: string | null;
  warnings: Array<string>;
};

export type AssessmentBlock = {
  label: string;
  value: string;
  basis: Array<string>;
  limitations: Array<string>;
};

export type Author = {
  id: string | null;
  handle: string | null;
  display_name: string | null;
  profile_url: string | null;
  bio: string | null;
  verified: boolean | null;
  followers_count: number | null;
  following_count: number | null;
  posts_count: number | null;
};

export type CaptionAssessment = "supported_by_context" | "contradicted_by_context" | "uncertain" | "not_provided" | "not_assessed";

export type ClipCoverageBlock = {
  label: string;
  value: string;
  description: string;
};

export type Comparison = {
  context_assessment: ContextAssessment;
  caption_assessment: CaptionAssessment;
  assessment_confidence: ConfidenceObject;
  summary: string;
  findings: Array<Finding>;
  limitations: Array<string>;
};

export type ConfidenceLevel = "high" | "moderate" | "low" | "not_assessed";

export type ConfidenceObject = {
  level: ConfidenceLevel;
  method: string;
  basis: Array<string>;
  limitations: Array<string>;
};

export type Context = {
  source_media_id: string;
  windows: Array<ContextWindow>;
  limitations: Array<string>;
};

export type ContextAssessment = "material_context_missing" | "no_material_difference_found" | "uncertain" | "not_assessed";

export type ContextWindow = {
  context_id: string;
  match_ids: Array<string>;
  requested_start_seconds: number;
  requested_end_seconds: number;
  coverage: "complete" | "partial" | "unavailable";
  sentences: Array<TranscriptSentence>;
};

export type DisplayModel = {
  source_match: AssessmentBlock | null;
  clip_coverage: ClipCoverageBlock | null;
  context_assessment: AssessmentBlock | null;
  confidence_note: string;
  no_source_message: string | null;
  context_unavailable_message: string | null;
  submitted_panel: MediaPanel;
  source_panel: MediaPanel | null;
  poster_info: PosterInfoBlock;
  matches: Array<Record<string, unknown>>;
  edit_analysis: Record<string, unknown> | null;
  findings: Array<Record<string, unknown>>;
  limitations: Array<string>;
};

export type EditAnalysis = {
  status: "assessed" | "not_assessed";
  scope: string;
  minimum_gap_seconds: number;
  source_gaps: Array<SourceGap>;
  unmatched_clip_ranges: Array<TimeRange>;
  counts: EditCounts;
};

export type EditCounts = {
  matched_passages: number | null;
  source_gaps: number | null;
  unmatched_clip_segments: number | null;
  confirmed_added_segments: number | null;
  confirmed_deleted_segments: number | null;
};

export type Engagement = {
  views_count: number | null;
  likes_count: number | null;
  comments_count: number | null;
  shares_count: number | null;
};

export type Evidence = {
  media_id: string;
  sentence_ids: Array<string>;
  start_seconds: number;
  end_seconds: number;
  quote: string;
};

export type Finding = {
  finding_id: string;
  type: string;
  explanation: string;
  evidence: Array<Evidence>;
};

export type Match = {
  match_id: string;
  clip_start_seconds: number;
  clip_end_seconds: number;
  source_start_seconds: number;
  source_end_seconds: number;
  evidence: Array<MatchEvidence>;
};

export type MatchEvidence = {
  method: string;
  clip_refs: Array<string>;
  source_refs: Array<string>;
  description: string;
};

export type MatchQuality = {
  confidence: ConfidenceObject;
  matched_clip_seconds: number | null;
  clip_coverage_ratio: number | null;
  metrics: Array<Record<string, unknown>>;
  methods_used: Array<string>;
};

export type MediaPanel = {
  post: Record<string, unknown>;
  transcript: Record<string, unknown> | null;
  media_playback_url: string | null;
};

export type Outcome = "matched" | "no_verified_match";

export type PosterInfo = {
  followers_count: number | string;
  following_count: number | string;
  posts_count: number | string;
};

export type PosterInfoBlock = {
  submitted: PosterInfo;
  source: PosterInfo | null;
};

export type PublicFrame = {
  frame_id: string;
  timestamp_seconds: number;
  image_url: string | null;
};

export type PublicMediaAsset = {
  media_id: string;
  mime_type: string;
  playback_url: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
};

export type PublicPost = {
  post_id: string;
  candidate_id: string | null;
  post_url: string;
  platform: string;
  title: string | null;
  caption: string | null;
  author: Author;
  engagement: Engagement;
  published_at: string | null;
  media: PublicMediaAsset;
  retrieval: Retrieval;
};

export type ReportMedia = {
  post: PublicPost;
  transcript: Transcript;
  frames: Array<PublicFrame>;
  visual_text: Array<VisualText>;
};

export type ReportV2 = {
  schema_version: "2.1";
  job_id: string;
  outcome: Outcome;
  submitted: ReportMedia;
  source: ReportMedia | null;
  alignment: Alignment | null;
  context: Context | null;
  comparison: Comparison | null;
  summary: string | null;
  limitations: Array<string>;
};

export type Retrieval = {
  retrieved_at: string;
  provider: string;
  metadata_source_urls: Array<string>;
  unavailable_fields: Array<string>;
  warnings: Array<string>;
};

export type SourceGap = {
  start_seconds: number;
  end_seconds: number;
  before_match_id: string;
  after_match_id: string;
};

export type TimeRange = {
  start_seconds: number;
  end_seconds: number;
};

export type Transcript = {
  status: TranscriptStatus;
  processed_ranges: Array<TimeRange>;
  sentences: Array<TranscriptSentence>;
};

export type TranscriptSentence = {
  sentence_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker_id: string | null;
};

export type TranscriptStatus = "complete" | "partial" | "unavailable";

export type VisualText = {
  frame_id: string;
  timestamp_seconds: number;
  text: string;
};
