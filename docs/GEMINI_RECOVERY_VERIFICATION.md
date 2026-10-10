# Gemini recovery and YouTube Test 3 — 2026-10-10

## Diagnosed failures

- `gemini-flash-latest` returned HTTP 504 even for a small structured JSON request.
- With the same configured key, `gemini-3.1-flash-lite` returned valid Comparison JSON.
  The ignored root `.env.local` now explicitly selects this model. No key was changed or committed.
- A subsequent real comparison joined two transcript sentences into one quotation.
  The strict local validator rejected it because each quotation must occur within one cited sentence.

## Changes

- Safe errors distinguish HTTP failures, timeouts, missing/empty responses, incomplete generation,
  and invalid JSON. Raw provider messages, response bodies, and credentials are never logged.
- Transient request failures allow three attempts, with 2/4-second backoff, a 45-second
  request timeout, and SDK retries disabled. Authentication/payment errors are not retried.
- Structured output has an explicit 8192-token allowance.
- Comparison explicitly requires separate quotations per sentence, exact IDs, and exact timestamps.
  One bounded correction request is allowed after rejected evidence. All local citation,
  schema, confidence, and coverage checks remain in force.

## Real Test 3 output

Ran the full public `run_analysis` function with `job_yt_001`,
`https://www.youtube.com/watch?v=jNQXAC9IVRw`, and no conversation ID.

```json
{
  "job_id": "job_yt_001",
  "report_id": "report_job_yt_001",
  "report_url": "http://localhost:3000/reports/report_job_yt_001",
  "outcome": "matched",
  "delivery_status": "skipped",
  "delivery_warning": null,
  "warnings": [
    "The submitted caption is an unverified search clue, not established fact.",
    "Candidate candidate_663e1df5 was skipped: The submitted video could not be retrieved.",
    "Candidate candidate_ebf60e4c was skipped: This post platform is not supported.",
    "Candidate candidate_b68738c4 was skipped: This post platform is not supported."
  ]
}
```

```text
PASS: full YouTube pipeline, report_id and report_url.
Elapsed seconds: 80.7
GET /reports/report_job_yt_001 -> HTTP 200
Source-match confidence: Moderate
Submitted clip aligned: 38%
Context-assessment confidence: Moderate
Findings: 1
Submitted/source transcript sentences: 2 5
Idempotency: same report_id, exactly 1 Tiger report row.
Tiger job: {'id': 'job_yt_001', 'status': 'completed', 'error_code': None, 'error_stage': None}
PASS: saved matched report, comparison, 3 confidence blocks, sanitization and idempotency.
```

The served report contains neither `local_path` nor `/tmp/`. Coverage is a measured
alignment ratio, not a confidence percentage. Three candidate sources were skipped,
as disclosed by the returned warnings; this test does not establish complete source coverage.

## Regression checks

```text
669 passed, 5 skipped, 45 subtests passed in 30.14s
Ruff: All checks passed!
```

Skipped checks include opt-in network tests and a Windows symlink-permission test.
The real YouTube pipeline above was run separately with network access enabled.
Photon delivery was intentionally skipped because this test supplies no conversation ID.
The report URL is local; public hosting and real iMessage acceptance remain separate gates.
