import type { DisplayModel, ReportV2 } from "./api-types";

export type DiagnosticValue = null | boolean | number | string | DiagnosticValue[] | { [key: string]: DiagnosticValue };

/** Separate from ReportV2 so diagnostics do not change the report contract. */
export interface ReportDiagnostics {
  job_id: string;
  capture_status: "recorded" | "unavailable";
  events: Array<{ stage: string; status: string; elapsed_seconds: number; data: Record<string, DiagnosticValue> }>;
  terminal_output: string;
}

export function diagnosticOutput(
  report: ReportV2 | null, display: DisplayModel | null, diagnostics: ReportDiagnostics | null, state: string,
  jobId = report?.job_id ?? diagnostics?.job_id ?? "unavailable",
): string {
  return [
    "ANALYSIS DIAGNOSTICS",
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
