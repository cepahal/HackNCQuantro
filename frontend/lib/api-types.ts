// Mirrors backend/services/shared/types.py and shared/errors.py field for field; that module is the source of truth.
// Backend fields defaulting to None are always serialized, so they are `T | null`, never optional.

export type ErrorCode =
  | "INVALID_URL"
  | "UNSUPPORTED_PLATFORM"
  | "MEDIA_UNAVAILABLE"
  | "TRANSCRIPTION_FAILED"
  | "SEARCH_FAILED"
  | "PROCESSING_FAILED"
  | "STORAGE_FAILED"
  | "MESSAGE_SEND_FAILED";

export type Stage =
  | "retrieve_post"
  | "extract_media_features"
  | "build_search_queries"
  | "search_candidates"
  | "retrieve_candidate_media"
  | "verify_and_align"
  | "select_surrounding_context"
  | "compare_context_and_caption"
  | "build_report"
  | "save_report_and_complete_job"
  | "display_report"
  | "send_summary"
  | "run_analysis";

export interface QuantroError {
  code: ErrorCode;
  stage: Stage;
  message: string;
  retryable: boolean;
}

export interface QuantroErrorEnvelope {
  error: QuantroError;
}

export type ConfidenceLevel = "high" | "moderate" | "low" | "not_assessed";
export type Outcome = "matched" | "no_verified_match";
export type TranscriptStatus = "complete" | "partial" | "unavailable";
export type ContextAssessment =
  | "material_context_missing"
  | "no_material_difference_found"
  | "uncertain"
  | "not_assessed";
export type CaptionAssessment =
  | "supported_by_context"
  | "contradicted_by_context"
  | "uncertain"
  | "not_provided"
  | "not_assessed";

// --- retrieve_post / retrieve_candidate_media -------------------------------

export interface Author {
  id: string | null;
  handle: string | null;
  display_name: string | null;
  profile_url: string | null;
  bio: string | null;
  verified: boolean | null;
  followers_count: number | null;
  following_count: number | null;
  posts_count: number | null;
}

export interface Engagement {
  views_count: number | null;
  likes_count: number | null;
  comments_count: number | null;
  shares_count: number | null;
}

/** MediaAsset as it may appear in a saved report: no worker filesystem path. */
export interface PublicMediaAsset {
  media_id: string;
  mime_type: string;
  playback_url: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_audio: boolean | null;
}

export interface MediaAsset extends PublicMediaAsset {
  local_path: string;
}

export interface Retrieval {
  retrieved_at: string;
  provider: string;
  metadata_source_urls: string[];
  unavailable_fields: string[];
  warnings: string[];
}

export interface PublicPost {
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
}

export interface Post extends PublicPost {
  media: MediaAsset;
}

// --- extract_media_features --------------------------------------------------

export interface TimeRange {
  start_seconds: number;
  end_seconds: number;
}

export interface TranscriptWord {
  text: string;
  start_seconds: number;
  end_seconds: number;
}

export interface TranscriptSentence {
  sentence_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  speaker_id: string | null;
  /** The backend serializer omits this key when there is no word timing. */
  words?: TranscriptWord[];
}

export interface Transcript {
  status: TranscriptStatus;
  processed_ranges: TimeRange[];
  sentences: TranscriptSentence[];
}

/** Frame as it may appear in a saved report: no worker filesystem path. */
export interface PublicFrame {
  frame_id: string;
  timestamp_seconds: number;
  image_url: string | null;
}

export interface Frame extends PublicFrame {
  image_path: string;
}

export interface VisualText {
  frame_id: string;
  timestamp_seconds: number;
  text: string;
}

export interface MediaFeatures {
  media_id: string;
  duration_seconds: number;
  language: string | null;
  transcript: Transcript;
  frames: Frame[];
  visual_text: VisualText[];
  warnings: string[];
}

// --- verify_and_align --------------------------------------------------------

export interface ConfidenceObject {
  level: ConfidenceLevel;
  method: string;
  basis: string[];
  limitations: string[];
}

export interface MatchEvidence {
  method: string;
  clip_refs: string[];
  source_refs: string[];
  description: string;
}

export interface Match {
  match_id: string;
  clip_start_seconds: number;
  clip_end_seconds: number;
  source_start_seconds: number;
  source_end_seconds: number;
  evidence: MatchEvidence[];
}

export interface MatchQuality {
  confidence: ConfidenceObject;
  matched_clip_seconds: number | null;
  clip_coverage_ratio: number | null;
  metrics: Record<string, unknown>[];
  methods_used: string[];
}

export interface SourceGap {
  start_seconds: number;
  end_seconds: number;
  before_match_id: string;
  after_match_id: string;
}

export interface EditCounts {
  matched_passages: number | null;
  source_gaps: number | null;
  unmatched_clip_segments: number | null;
  confirmed_added_segments: number | null;
  confirmed_deleted_segments: number | null;
}

export interface EditAnalysis {
  status: "assessed" | "not_assessed";
  scope: string;
  minimum_gap_seconds: number;
  source_gaps: SourceGap[];
  unmatched_clip_ranges: TimeRange[];
  counts: EditCounts;
}

export interface Alignment {
  status: "verified" | "no_verified_match" | "not_assessed";
  clip_media_id: string;
  source_media_id: string;
  matches: Match[];
  match_quality: MatchQuality;
  edit_analysis: EditAnalysis;
  limitations: string[];
}

// --- select_surrounding_context ----------------------------------------------

export interface ContextWindow {
  context_id: string;
  match_ids: string[];
  requested_start_seconds: number;
  requested_end_seconds: number;
  coverage: "complete" | "partial" | "unavailable";
  sentences: TranscriptSentence[];
}

export interface Context {
  source_media_id: string;
  windows: ContextWindow[];
  limitations: string[];
}

// --- compare_context_and_caption ---------------------------------------------

export type AssessmentConfidence = ConfidenceObject;

export interface Evidence {
  media_id: string;
  sentence_ids: string[];
  start_seconds: number;
  end_seconds: number;
  quote: string;
}

export interface Finding {
  finding_id: string;
  type: string;
  explanation: string;
  evidence: Evidence[];
}

export interface Comparison {
  context_assessment: ContextAssessment;
  caption_assessment: CaptionAssessment;
  assessment_confidence: AssessmentConfidence;
  summary: string;
  findings: Finding[];
  limitations: string[];
}

// --- report ------------------------------------------------------------------

export interface ReportMedia {
  post: PublicPost;
  transcript: Transcript;
  frames: PublicFrame[];
  visual_text: VisualText[];
}

export interface ReportV2 {
  schema_version: "2.1";
  job_id: string;
  outcome: Outcome;
  submitted: ReportMedia;
  source: ReportMedia | null;
  alignment: Alignment | null;
  context: Context | null;
  comparison: Comparison | null;
  summary: string | null;
  limitations: string[];
}

export interface SavedReportRef {
  job_id: string;
  report_id: string;
  report_url: string;
}

export interface JobCompletionResult extends SavedReportRef {
  status: "completed";
  completed_at: string;
}

export interface DeliveryResult {
  conversation_id: string;
  message_id: string;
  status: "accepted";
}

// --- search, orchestration, and public display --------------------------------

export interface IngestResult {
  job_id: string;
  url: string;
  post_id: string;
  media_id: string;
  transcript_status: TranscriptStatus;
  sentence_count: number;
  frames_count: number;
  persisted_at: string;
}

export interface AnalysisResult extends SavedReportRef {
  outcome: Outcome;
  delivery_status: "accepted" | "failed" | "skipped";
  delivery_warning: string | null;
  warnings: string[];
}

export interface AssessmentBlock {
  label: string;
  value: string;
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
  followers_count: number | string;
  following_count: number | string;
  posts_count: number | string;
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
