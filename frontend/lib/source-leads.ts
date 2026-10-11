import type { ReportV2 } from "./api-types.ts";
import { youtubePreviewUrl } from "./youtube-preview.ts";

const URL_PREFIX = "Unverified source candidate: ";
const DETAILS_PREFIX = "Unverified source candidate details: ";
const REASON = "Search found this recording as a possible lead. Its speech and broader context have not been verified.";
export type SourceLead = { url: string; title: string; reason: string; preview: string };

function canonical(value: unknown): { url: string; preview: string } | null {
  if (typeof value !== "string") return null;
  const preview = youtubePreviewUrl(value);
  if (!preview) return null;
  const id = new URL(preview).pathname.slice("/embed/".length);
  return { url: `https://www.youtube.com/watch?v=${id}`, preview };
}

function plain(value: unknown, limit: number): string | null {
  return typeof value === "string" && value.trim()
    ? value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\*\*/g, "").replace(/\s*\u2014\s*/g, ", ").trim().slice(0, limit) : null;
}

/** Discovery leads remain separate from a verified source and its confidence. */
export function sourceLeads(report: ReportV2): SourceLead[] {
  const notes = [...report.limitations, ...report.submitted.post.retrieval.warnings];
  const excluded = new Set([
    canonical(report.submitted.post.post_url)?.url,
    canonical(report.source?.post.post_url)?.url,
  ]);
  const details = new Map<string, { title: string | null; reason: string | null }>();
  for (const note of notes) {
    if (!note.startsWith(DETAILS_PREFIX) || note.length > 2_000) continue;
    try {
      const data: unknown = JSON.parse(note.slice(DETAILS_PREFIX.length));
      if (!data || typeof data !== "object" || Array.isArray(data)) continue;
      const item = data as Record<string, unknown>;
      const safe = canonical(item.url);
      if (safe) details.set(safe.url, { title: plain(item.title, 200), reason: plain(item.reason, 300) });
    } catch { /* A malformed optional annotation cannot break the report. */ }
  }
  const leads = new Map<string, SourceLead>();
  for (const note of notes) {
    if (!note.startsWith(URL_PREFIX)) continue;
    const safe = canonical(note.slice(URL_PREFIX.length));
    if (!safe || excluded.has(safe.url) || leads.has(safe.url)) continue;
    const detail = details.get(safe.url);
    leads.set(safe.url, { ...safe, title: detail?.title ?? "Possible related recording", reason: detail?.reason ?? REASON });
    if (leads.size === 5) break;
  }
  return [...leads.values()];
}
