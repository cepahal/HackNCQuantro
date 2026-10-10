import type { ReportMedia, ReportV2 } from "../../lib/api-types.ts";

function reportMedia(id: string, url: string, caption: string | null): ReportMedia {
  return {
    post: {
      post_id: `post_${id}`,
      candidate_id: id === "source" ? "candidate_1" : null,
      post_url: url,
      platform: "youtube",
      title: `Title ${id}`,
      caption,
      author: {
        id: null, handle: `@${id}`, display_name: null, profile_url: null, bio: null, verified: null,
        followers_count: 1200, following_count: null, posts_count: null,
      },
      engagement: { views_count: 5000, likes_count: null, comments_count: null, shares_count: null },
      published_at: "2026-10-01T12:00:00+00:00",
      media: {
        media_id: `media_${id}`, mime_type: "video/mp4", playback_url: null, duration_seconds: 42.5,
        width: 1280, height: 720, fps: 30, has_audio: true,
      },
      retrieval: {
        retrieved_at: "2026-10-10T17:00:00+00:00", provider: "yt-dlp", metadata_source_urls: [url],
        unavailable_fields: ["author.following_count"], warnings: [],
      },
    },
    transcript: {
      status: "complete",
      processed_ranges: [{ start_seconds: 0, end_seconds: 42.5 }],
      sentences: [
        { sentence_id: `${id}_s1`, start_seconds: 1, end_seconds: 4.5, text: "We will not raise taxes.", speaker_id: null },
        {
          sentence_id: `${id}_s2`, start_seconds: 4.5, end_seconds: 6, text: "Unless we must.", speaker_id: null,
          words: [{ text: "Unless", start_seconds: 4.5, end_seconds: 5 }, { text: "we", start_seconds: 5, end_seconds: 5.3 }],
        },
      ],
    },
    frames: [{ frame_id: `${id}_f1`, timestamp_seconds: 2, image_url: null }],
    visual_text: [{ frame_id: `${id}_f1`, timestamp_seconds: 2, text: "LIVE" }],
  };
}

const confidence = { level: "moderate", method: "transcript_alignment", basis: ["Two aligned sentences."], limitations: [] } as const;

export const reportFixture = {
  schema_version: "2.1",
  job_id: "job_1",
  outcome: "matched",
  submitted: reportMedia("submitted", "https://www.tiktok.com/@clip/video/1", "He promised no new taxes"),
  source: reportMedia("source", "https://www.youtube.com/watch?v=source", null),
  alignment: {
    status: "verified",
    clip_media_id: "media_submitted",
    source_media_id: "media_source",
    matches: [{
      match_id: "m1", clip_start_seconds: 1, clip_end_seconds: 4.5, source_start_seconds: 101, source_end_seconds: 104.5,
      evidence: [{ method: "transcript_alignment", clip_refs: ["submitted_s1"], source_refs: ["source_s1"], description: "Same sentence." }],
    }],
    match_quality: {
      confidence: { ...confidence, basis: [...confidence.basis], limitations: [] },
      matched_clip_seconds: 3.5, clip_coverage_ratio: 0.5, metrics: [{ name: "token_overlap", value: 0.92 }], methods_used: ["transcript_alignment"],
    },
    edit_analysis: {
      status: "assessed", scope: "Within matched passages only.", minimum_gap_seconds: 1,
      source_gaps: [], unmatched_clip_ranges: [{ start_seconds: 4.5, end_seconds: 6 }],
      counts: {
        matched_passages: 1, source_gaps: 0, unmatched_clip_segments: 1,
        confirmed_added_segments: null, confirmed_deleted_segments: null,
      },
    },
    limitations: [],
  },
  context: {
    source_media_id: "media_source",
    windows: [{
      context_id: "c1", match_ids: ["m1"], requested_start_seconds: 71, requested_end_seconds: 134.5, coverage: "complete",
      sentences: [{ sentence_id: "source_s1", start_seconds: 101, end_seconds: 104.5, text: "We will not raise taxes.", speaker_id: null }],
    }],
    limitations: [],
  },
  comparison: {
    context_assessment: "material_context_missing",
    caption_assessment: "contradicted_by_context",
    assessment_confidence: { ...confidence, basis: [...confidence.basis], limitations: [] },
    summary: "The clip omits the qualifying sentence that follows.",
    findings: [{
      finding_id: "f1", type: "omitted_qualifier", explanation: "The source continues with a condition.",
      evidence: [{ media_id: "media_source", sentence_ids: ["source_s1"], start_seconds: 101, end_seconds: 104.5, quote: "We will not raise taxes." }],
    }],
    limitations: [],
  },
  summary: "The clip omits the qualifying sentence that follows.",
  limitations: ["Transcript timestamps are estimates."],
} satisfies ReportV2;
