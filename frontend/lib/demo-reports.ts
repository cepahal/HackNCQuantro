import { youtubeVideoId } from "./youtube-preview.ts";

/**
 * Saved pipeline results for the live-demo links, served from public/demo/<video id>/.
 * Matching is by YouTube video ID so watch, shorts and youtu.be variants all hit the demo path.
 */
const DEMOS: Record<string, { jobId: string; media: Record<string, string> }> = {
  qzYTN4lfEn4: { jobId: "job_demo_qzYTN4lfEn4_5", media: {
    media_df681e20ed5ea718c655a415: "submitted.mp4", context_eda9ad93e0fb4fa286154722abfbf32e: "context.mp4",
  } },
  tSj9gkgCM8Q: { jobId: "job_demo_tSj9gkgCM8Q_5", media: {
    media_3bd11d4bca84cf1958566b53: "submitted.mp4", context_c9e6117cc504433ca30945804dfbb761: "context.mp4",
  } },
  "3XPuGqqWLtk": { jobId: "job_demo_3XPuGqqWLtk_5", media: {
    media_2a5f2f764c267776fd661b63: "submitted.mp4", context_5c1fbce551014940a561c688ae7b58ee: "context.mp4",
  } },
};

export const DEMO_DELAY_MS = 5000;

const started = new Map<string, number>();

function videoIdForJob(jobId: string): string | null {
  return Object.keys(DEMOS).find(id => DEMOS[id].jobId === jobId) ?? null;
}

export function demoJobForUrl(url: string): string | null {
  const id = youtubeVideoId(url.trim());
  return id && Object.hasOwn(DEMOS, id) ? DEMOS[id].jobId : null;
}

export function isDemoJob(jobId: string): boolean {
  return videoIdForJob(jobId) !== null;
}

export function markDemoStarted(jobId: string): void {
  started.set(jobId, Date.now());
}

/** Fraction of the demo delay elapsed; 1 when the run began before this page load. */
export function demoProgressFraction(jobId: string): number {
  const start = started.get(jobId);
  return start === undefined ? 1 : Math.min(1, (Date.now() - start) / DEMO_DELAY_MS);
}

export function demoAssetPath(jobId: string, name: "result" | "report" | "display" | "diagnostics" | "interpretation" | "progress"): string | null {
  const id = videoIdForJob(jobId);
  return id ? `/demo/${id}/${name}.json` : null;
}

/** Maps a report API path for a demo report to its saved JSON file. */
export function demoReportPath(path: string): string | null {
  const match = /^\/reports\/report_([A-Za-z0-9_-]{1,128})(?:\/(display|diagnostics|interpretation))?$/.exec(path);
  if (!match) return null;
  return demoAssetPath(match[1], (match[2] ?? "report") as "report" | "display" | "diagnostics" | "interpretation");
}

export function demoMediaPath(mediaId: string): string | null {
  for (const id of Object.keys(DEMOS)) {
    const file = DEMOS[id].media[mediaId];
    if (file) return `/demo/${id}/${file}`;
  }
  return null;
}
