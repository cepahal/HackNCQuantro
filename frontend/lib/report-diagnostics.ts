import type { DisplayModel, ReportV2 } from "./api-types";

export type DiagnosticValue = null | boolean | number | string | DiagnosticValue[] | { [key: string]: DiagnosticValue };

/** Separate from ReportV2 so diagnostics do not change the report contract. */
export interface ReportDiagnostics {
  job_id: string;
  capture_status: "recorded" | "unavailable";
  events: Array<{ stage: string; status: string; elapsed_seconds: number; data: Record<string, DiagnosticValue> }>;
  terminal_output: string;
}

export function analysisName(report: ReportV2 | null): string {
  const title = report?.submitted.post.title?.trim().replace(/\s+/g, " ") ?? "";
  const identifier = /^(?:job|report)(?:[_-][a-z0-9_-]+|[:\s]+\d+)?$/i;
  const uuid = /^(?:urn:uuid:)?\{?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\}?$/i;
  if (!title || !/[\p{L}\p{N}]/u.test(title) || identifier.test(title) || uuid.test(title)
    || /^[a-f0-9]{24,64}$/i.test(title) || title === report?.job_id) return "Video analysis";
  return title;
}

export function diagnosticFilename(report: ReportV2 | null): string {
  const slug = analysisName(report).normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100).replace(/-+$/g, "") || "video";
  return `${slug}${/(?:^|-)analysis$/.test(slug) ? "" : "-analysis"}.txt`;
}

export function diagnosticOutput(
  report: ReportV2 | null, display: DisplayModel | null, diagnostics: ReportDiagnostics | null, state: string,
  jobId = report?.job_id ?? diagnostics?.job_id ?? "unavailable",
): string {
  return [
    `${analysisName(report)} | ANALYSIS DIAGNOSTICS`,
    `Job: ${jobId}`,
    `Outcome: ${report?.outcome ?? "Report unavailable"}`,
    "",
    "=== CAPTURED RESOLVER OUTPUT ===",
    diagnostics?.capture_status === "recorded"
      ? diagnostics.terminal_output
      : state,
    "",
    "=== ALL SAVED ANALYSIS DATA (JSON) ===",
    JSON.stringify({
      report,
      display,
      diagnostics,
    }, null, 2),
  ].join("\n");
}
