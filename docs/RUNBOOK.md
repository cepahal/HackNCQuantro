# Quantro Runbook

Owner: Bhrigu (integration and reporting: `build_report`, `save_report_and_complete_job`,
`display_report`, `send_summary`, `run_analysis`). The flow, function owners, env var
owners and failure modes are in [INTEGRATION.md](INTEGRATION.md).

## 1. Setup

### Backend (Python 3.11+)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-whisper.txt
cd ..
mkdir -p .secrets && chmod 700 .secrets
cp .env.example .secrets/.env && chmod 600 .secrets/.env   # tracked blank template -> fill in the copy
ln -s .secrets/.env .env.local                             # teammates' modules and the Photon sidecar read .env.local
```

Secrets live in the repo-root `.secrets/.env` or `.env.local`, both git-ignored.
On Windows, `.env.local` may be a regular private file instead of a symlink.
The API and CLI read both locations; `.env.example` remains a blank tracked template.
The private environment needs at least:

```bash
TIGER_DATABASE_URL=postgres://<user>:<password>@<host>:<port>/tsdb?sslmode=require
TIGER_TABLE_PREFIX=quantro_
REPORT_STORE=tiger
REPORT_BASE_URL=http://localhost:3000
GEMINI_API_KEY=<key>
GEMINI_MODEL=gemini-flash-latest
SEARCH_PROVIDER=exa
EXA_API_KEY=<key>
TRANSCRIPTION_PROVIDER=whisper
WHISPER_MODEL=small.en
```

Provider behavior and limits (Exa search, local Whisper) are in [PROVIDERS.md](PROVIDERS.md).

Retrieval and search also need `TIGERDATA_DSN` (the same Tiger URL) and
`TIGERDATA_SSL_ROOT_CERT=system`, or the absolute path to the service's trusted CA
bundle on systems that need it. The operational store falls back to `TIGERDATA_DSN`
when `TIGER_DATABASE_URL` is absent, with full certificate and hostname verification.
See [retrieve_post.md](retrieve_post.md) and
[source_search.md](source_search.md).

### Verify the Tiger Data connection

The URL is read from the vault so it never lands in shell history:

```bash
cd backend
psql "$(grep '^TIGER_DATABASE_URL=' ../.secrets/.env | cut -d= -f2-)" -c 'select 1'
python -m services.storage.tiger list     # recent jobs as JSON ([] on a fresh database)
```

The API creates `quantro_jobs`, `quantro_posts` and `quantro_reports` on first boot.
Production `REPORT_STORE` supports `tiger` only; tests can inject in-memory stores; use Tiger Data for the MVP.

### Frontend (Node 24+)

```bash
cd frontend
npm install
npm run dev                 # http://localhost:3000
```

The Next.js app includes URL submission and `/reports/{report_id}` rendering. For
a production demo, run `npm run build` followed by `npm start`. Its API origin is
`NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`); set it in private
`frontend/.env.local` before building when deploying to another origin.

## 2. Smoke test

`tests/test_smoke_e2e.py` drives `/ingest`, `/analyze/{job_id}/run` and `/reports/{id}`
through the real ASGI app against a real Postgres. It replaces the eight upstream
functions with contract-shaped fixtures and deletes the rows it writes. It is skipped
when `TIGER_DATABASE_URL` is unset.

Against a local Postgres (no TLS needed on localhost), from the repo root:

```bash
createdb quantro_smoke      # once
TIGER_DATABASE_URL=postgresql://localhost:5432/quantro_smoke pytest tests/test_smoke_e2e.py -v
```

Against Tiger Data, using `.secrets/.env`:

```bash
pytest tests/test_smoke_e2e.py -v
```

## 3. Run the demo

```bash
# terminal 1: API
cd backend && source .venv/bin/activate
uvicorn main:app --port 8000

# terminal 2: Photon bridge (iMessage replies); `start` = terminal mode, no Spectrum keys needed
npm --prefix backend/services/photon ci --ignore-scripts   # once
npm --prefix backend/services/photon run start:imessage    # needs SPECTRUM_PROJECT_ID/SECRET

# terminal 3: frontend
cd frontend && npm run dev
```

| URL | What |
|---|---|
| http://localhost:8000/docs | API explorer (`/ingest/{job_id}`, `/analyze/{job_id}/run`, `/reports/{report_id}`) |
| http://localhost:8000/reports/report_demo_001 | Raw sanitized report JSON |
| http://localhost:3000 | Submission page (frontend) |
| http://localhost:3000/reports/report_demo_001 | Report page (frontend). This is the link sent over iMessage. |

`uvicorn backend.main:app --port 8000` from the repo root is equivalent.

### curl

Ingest only. This retrieves the post, extracts features and stores both in Tiger Data:

```bash
curl -sS -X POST http://localhost:8000/ingest/demo_001 \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# {"job_id":"demo_001","url":"…","post_id":"post_…","media_id":"media_…",
#  "transcript_status":"complete","sentence_count":12,"frames_count":12,"persisted_at":"…Z"}
```

Full analysis. This runs ingest itself, so the call above is optional. Leave out
`conversation_id` to skip the iMessage reply:

```bash
curl -sS -X POST http://localhost:8000/analyze/demo_001/run \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "conversation_id": "demo-conversation"}'
# {"job_id":"demo_001","outcome":"matched","report_id":"report_demo_001",
#  "report_url":"http://localhost:3000/reports/report_demo_001",
#  "delivery_status":"accepted","delivery_warning":null,"warnings":[]}
```

Fetch the report, then inspect the job:

```bash
curl -sS http://localhost:8000/reports/report_demo_001
cd backend && python -m services.storage.tiger show demo_001
```

Errors come back as `{"error": {"code", "stage", "message", "retryable"}}`. Status
codes: 400 bad URL or platform, 404 media unavailable, 502 transcription failed, 500
search, processing or storage failure, 422 bad request body or job ID. Re-running the
same `job_id` is safe and returns the same `report_id`.
Completed jobs reuse their saved report and skip expensive upstream analysis.
Use a new job ID to analyze another URL. Failed retrievals remain visible as failed
jobs, and browser-facing ingest JSON omits local media and frame file paths.

## 4. Verify before pushing

Run from the repo root unless noted.

```bash
cd backend && python -c "from services.shared.types import ReportV2; print('types ok')" && cd ..
pytest tests/ -v
cd backend && uvicorn main:app --port 8000          # boots clean, Ctrl-C to stop
cd backend && python -m services.storage.tiger list # real Tiger Data
grep -r "/tmp/\|local_path\|api_key\|secret" backend/services/reporting/
```

The grep is never empty, and that is expected. `build_report.py` names the keys and
path prefixes its sanitizer strips, and docstrings describe the rule. A match in any
other file is a real finding. To list only lines that are not the denylist:

```bash
grep -rn --include='*.py' "/tmp/\|local_path\|api_key\|secret" backend/services/reporting/ \
  | grep -v -e '^backend/services/reporting/build_report.py:'
```

## 5. Pre-demo checklist

- [ ] `.secrets/.env` has a valid `TIGER_DATABASE_URL` (one line, `sslmode=require`) and a rotated password
- [ ] `psql … -c 'select 1'` and `python -m services.storage.tiger list` both succeed
- [ ] `GEMINI_API_KEY` set; `which ffmpeg ffprobe` finds both
- [ ] `TIGERDATA_DSN` set in `.secrets/.env`, `.env.local` links to it, and the search corpus is seeded with the demo source video
- [ ] `pytest tests/ -v` is green, including `test_smoke_e2e.py` against local Postgres
- [ ] `uvicorn main:app --port 8000` boots with no traceback; `/docs` lists all three routes
- [ ] Photon bridge is up (`curl http://127.0.0.1:8766/health`) and `SPECTRUM_PROJECT_ID`/`SECRET` are in the vault for real iMessage
- [ ] `npm run dev` serves http://localhost:3000, and `/reports/[id]` exists (the link is `/reports/`, not `/report/`)
- [ ] One full curl run of the demo URL returns `outcome: "matched"`, and the report page opens
- [ ] `git status` shows no `.env` files staged
- [ ] Screenshot the report page and save the curl output as a fallback

## 6. Deploy

Keep the Next.js frontend on Vercel and deploy the existing FastAPI Docker image
on a host with enough RAM and outbound PostgreSQL access. See [HOSTING.md](HOSTING.md)
for verified Railway/Render/free-tier limits and conditional cost estimates.
No paid host has been selected or purchased by this integration change.

- The checked-in Render Blueprint builds `backend/Dockerfile.vercel` with `backend/`
  as its Docker context. Its existing free plan is not a verified Whisper deployment:
  the recorded small.en run used about 980 MiB before API/media overhead, while
  Render free has 512 MB. Railway's service trial/free tiers also require capacity
  review. The Docker command binds `0.0.0.0` and the platform's `$PORT`.
- API environment: `APP_ENV=production`, `REPORT_STORE=tiger`, `SEARCH_PROVIDER=exa`,
  `EXA_API_KEY`, `TIGER_DATABASE_URL`, `TIGERDATA_DSN`, `TIGER_TABLE_PREFIX`,
  `TIGERDATA_SSL_ROOT_CERT`, `GEMINI_API_KEY`, `GEMINI_MODEL`, and
  `TRANSCRIPTION_PROVIDER=whisper`. The image includes its Whisper model; keep
  `WHISPER_MODEL` at the baked model path. Supply credentials only through private
  hosting settings. The actual Tiger CA must validate the deployment's connection.
- Set `REPORT_BASE_URL=https://quantro-ten.vercel.app` for report links and
  `FRONTEND_ORIGIN=https://quantro-ten.vercel.app` for browser CORS. These settings
  are independent; a report-sharing gateway is not an analysis API.
- Verify the assigned HTTPS backend URL returns this app's `/openapi.json` with
  `/analyze/{job_id}/run`, `/analyze/{job_id}/status`, `/reports/{report_id}`, and
  `/ingest/{job_id}`. Do not guess a hostname: `quantro-api.onrender.com` was checked
  and belongs to an unrelated trading API.
- Vercel production currently builds the `frontend/` service using `vercel.json`.
  Set `NEXT_PUBLIC_API_BASE_URL` to the verified backend HTTPS origin, without an
  `/api` suffix. Optionally set `NEXT_PUBLIC_REPORT_API_BASE_URL` only when using a
  separate read-only report gateway. The frontend uses direct `process.env` access
  so Next.js embeds these public values at build time; redeploy after changes.
  A missing production backend setting now raises an explicit configuration error
  rather than silently calling the visitor's localhost. Node is pinned to `24.x`.
- The Spectrum Node listener is a separate long-running process and remains the
  primary iMessage workflow. `PHOTON_BRIDGE_TOKEN` authenticates its private summary
  bridge; a deployed backend also needs a reachable `PHOTON_BRIDGE_URL` if it sends
  summaries through that bridge. Its loopback default is not reachable from a
  remote container. Web analysis without a conversation ID does not send messages.
- The optional archived AdvancedKit HTTP webhook transport uses its own
  `PHOTON_WEBHOOK_SECRET` and documented signed timestamp. Production rejects the
  legacy send-key signature. This transport's outbound `/messages` API is not
  verified against a live provider; do not substitute it for tested Spectrum.

## 7. Failure recovery and regression gate

Start the healthy backend first, then managed Photon. On every managed startup,
Photon reconciles existing pending/uncertain/failed markers through GET requests.
This runs in the background, so incoming acknowledgments do not wait for recovery.
A valid saved report confirms completion even if the analysis HTTP response failed.
A failed backend job supplies its safe error code/stage. Unknown or running work is
never reported as completed or automatically replayed. Recovery never resends a
report or failure notice for an existing marker.

Default startup does not process old marker-free inbox records. After reviewing
saved intake and obtaining approval for any external processing/delivery, an
operator can explicitly resume only provably never-started URL requests:

```bash
npm --prefix backend/services/photon run start:imessage -- --resume-saved
```

Use one listener instance. Recovery validates native intake/event hashes, requires
both job and report lookups to return the expected absence responses, resolves the
original conversation, and writes an exclusive durable marker before queuing a POST.
It scans at most 256 directory entries / 32 jobs with a 60-second lookup budget.
Inspect unresolved/limited recovery manually; do not delete existing markers or
delivery ledgers to force a replay. Leave the flag off for ordinary restarts.

Same-post sources are rejected at selection, report construction, cached reuse,
serving, display, and final delivery. A legacy invalid report returns structured
PROCESSING_FAILED; evidence in Tiger is preserved. Use a fresh job for a corrected
analysis. Definitive caption assessments require a separate transcript-grounding
check with valid source citations. Missing evidence, disagreement, or an unavailable
secondary check yields Not assessed. Two model interpretations can still share
reasoning errors; citations establish provenance, not universal logical correctness.

`.github/workflows/reliability.yml` checks every push and pull request with isolated
offline backend tests, dependency/startup checks, both Node suites/typechecks, and
a frontend production build. It supplies no provider credentials and removes
NETWORK. Live platform access, real Tiger semantics, message receipt, and mobile
report rendering remain separate acceptance tests.

Recovery limits apply across both marker and inbox scans. A large marker history
can exhaust the scan before the inbox is reached. Startup logs `limited=true`
and a manual-review instruction in that case; do not treat a truncated scan as
a fully drained queue. Automated pagination of large backlogs is not implemented.
