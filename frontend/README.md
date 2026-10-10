# Quantro web MVP

Next.js App Router frontend for the existing FastAPI analysis and saved-report endpoints.
Requires Node 24. The UI is Grant's design (`app/globals.css`, Tailwind 4 via PostCSS, and
framer-motion for the homepage algorithm animation) on Next.js, React, and TypeScript.

```powershell
npm --prefix frontend ci
npm --prefix frontend run dev
```

Open `http://localhost:3000`. Start the backend on port 8000 using the repository runbook.
Set backend `REPORT_BASE_URL=http://localhost:3000` so saved report links and CORS use the same origin.
For deployment, set that variable to the actual public frontend origin. Configure
`NEXT_PUBLIC_API_BASE_URL` in private `frontend/.env.local` before building if the browser-visible
backend is not `http://localhost:8000`. This is a public URL, never a credential.

The "Try now" dialog starts `POST /analyze/{job_id}/run` with `{url}` and opens
`/results?job={job_id}`, which shows the loading state until the request returns and then
replaces the URL with `/results?id={report_id}` and reads `GET /reports/{report_id}`.
`/reports/{report_id}` (the backend's iMessage link) and `/results/{report_id}` redirect there.
Video file upload is not supported by the backend yet; the dialog asks for a post link instead.
Reports show source evidence, available transcripts, source-match and context-assessment
confidence separately, clip coverage, and limitations. JPG/MP4 playback uses a returned
`playback_url`, or the backend `GET /media/{media_id}` endpoint. The backend remains responsible
for URL validation, provider calls, persistence, and safe public report serialization.

```powershell
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend start
```

Source discovery uses Exa when SEARCH_PROVIDER=exa, or the indexed Tiger Data corpus when SEARCH_PROVIDER=tiger. A no-match result is a
valid evidence-limited result. The UI does not treat missing evidence as proof that a post is false. The MVP starts requests synchronously. After a page refresh, the results page polls
GET /analyze/{job_id}/status and opens the saved report when ready without resubmitting.
Polling stops after about 15 minutes; refresh to check again. A backend restart does not
resume interrupted jobs automatically. Authentication and access control for private submissions must be
added before hosting submissions for people outside the demo team.

The passage map labels approximate source gaps rather than confirmed deletions. Surrounding
context windows show their requested intervals and transcript coverage explicitly.
