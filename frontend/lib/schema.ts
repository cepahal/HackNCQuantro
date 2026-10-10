import { z } from "zod";

const nullableString = z.string().nullable().optional();
const timeRange = z.object({ start_seconds: z.number().nonnegative(), end_seconds: z.number().nonnegative() }).passthrough();
const sentence = z.object({ sentence_id: z.string(), start_seconds: z.number().nonnegative(), end_seconds: z.number().nonnegative(), text: z.string(), speaker_id: nullableString }).passthrough();
const transcript = z.object({ status: z.enum(["complete", "partial", "unavailable"]), processed_ranges: z.array(timeRange), sentences: z.array(sentence) }).passthrough();
const author = z.object({ id: nullableString, handle: nullableString, display_name: nullableString, profile_url: nullableString, bio: nullableString, verified: z.boolean().nullable().optional(), followers_count: z.number().nullable().optional(), following_count: z.number().nullable().optional(), posts_count: z.number().nullable().optional() }).passthrough();
const post = z.object({ post_id: z.string(), post_url: z.string(), platform: z.string(), title: nullableString, caption: nullableString, author, engagement: z.object({}).passthrough(), published_at: nullableString, media: z.object({ media_id: z.string(), mime_type: z.string(), playback_url: nullableString, duration_seconds: z.number().nullable(), width: z.number().nullable(), height: z.number().nullable(), fps: z.number().nullable(), has_audio: z.boolean().nullable() }).passthrough(), retrieval: z.object({ retrieved_at: z.string(), provider: z.string(), metadata_source_urls: z.array(z.string()), unavailable_fields: z.array(z.string()), warnings: z.array(z.string()) }).passthrough() }).passthrough();
const reportSchema = z.object({ schema_version: z.string(), job_id: z.string(), outcome: z.enum(["matched", "no_verified_match"]), submitted: z.object({ post, transcript, frames: z.array(z.object({ frame_id: z.string(), timestamp_seconds: z.number(), image_url: nullableString }).passthrough()).optional(), visual_text: z.array(z.object({}).passthrough()).optional() }).passthrough(), source: z.object({ post, transcript }).passthrough().nullable(), alignment: z.object({}).passthrough().nullable(), context: z.object({}).passthrough().nullable(), comparison: z.object({}).passthrough().nullable(), summary: nullableString, limitations: z.array(z.string()) }).passthrough();

export function parseReport(value: unknown) {
  const result = reportSchema.safeParse(value);
  if (!result.success) return { success: false as const, issues: result.error.issues };
  return { success: true as const, data: result.data as unknown as import("./types").Report };
}
