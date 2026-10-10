# Quantro web MVP

Next.js App Router frontend for the existing FastAPI analysis and saved-report endpoints.
Requires Node 24. Dependencies are Next.js, React, and TypeScript; there is no separate UI framework.

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

Open `http://localhost:3000`. Start the backend on port 8000 using the repository runbook.
Set backend `REPORT_BASE_URL=http://localhost:3000` so saved report links and CORS use the same origin.
For deployment, set that variable to the actual public frontend origin. Configure
`NEXT_PUBLIC_API_BASE_URL` in private `frontend/.env.local` before building if the browser-visible
backend is not `http://localhost:8000`. This is a public URL, never a credential.

The home page submits `{url}` to `POST /analyze/{job_id}/run`, then reads the saved ReportV2
from `GET /reports/{report_id}`. Shared frontend links use `/reports/{report_id}`.
Reports show source evidence, available transcripts, source-match and context-assessment
confidence separately, clip coverage, and limitations. JPG/MP4 playback uses a returned
`playback_url`, or the backend `GET /media/{media_id}` endpoint. The backend remains responsible
for URL validation, provider calls, persistence, and safe public report serialization.

```powershell
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend start
```

Source discovery currently searches the indexed Tiger Data corpus. A no-match result is a
valid evidence-limited result. This UI does not claim public-web coverage or treat missing
evidence as proof that a post is false. The MVP runs requests synchronously; keep the tab open
while analysis completes. Authentication and access control for private submissions must be
added before hosting submissions for people outside the demo team.
