# Quantro Runbook

Owner: Bhrigu (integration / reporting: `build_report`, `save_report_and_complete_job`,
`display_report`, `send_summary`, `run_analysis`).

## 1. Install

### Backend (Python 3.11+)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in keys; never commit .env
```

### Frontend (Node 20+)

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000
```

As of this writing `frontend/package.json` is an empty placeholder, so these commands
fail until the Next.js app is initialized (frontend owner).

## 2. Environment template (`backend/.env.example`)

```bash
REPORT_BASE_URL=http://localhost:3000
REPORT_STORE=memory
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=
SNOWFLAKE_PASSWORD=
SNOWFLAKE_DATABASE=
SNOWFLAKE_SCHEMA=
SNOWFLAKE_WAREHOUSE=
SPECTRUM_PROJECT_ID=
SPECTRUM_PROJECT_SECRET=
PHOTON_BRIDGE_TOKEN=
PHOTON_BRIDGE_PORT=8766
PHOTON_DATA_DIR=.data/photon
RETRIEVAL_PROVIDER_API_KEY=
SEARCH_PROVIDER_API_KEY=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-1.5-pro
TRANSCRIPTION_API_KEY=
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_REPORT_BASE_URL=http://localhost:3000
```

`REPORT_STORE` is `memory` (default) or `snowflake` (not implemented yet). In-memory
reports and job statuses are lost when the backend restarts. Photon delivery records
persist in `PHOTON_DATA_DIR`; keep that directory across transport restarts. See
[Photon setup](../backend/services/photon/README.md) for Node 24 and the private bridge.

## 3. Verify

Run from the repo root unless noted.

```bash
# Contract types import (requires ReportV2 in types.py)
cd backend && python -c "from services.reporting.types import ReportV2; print('types ok')" && cd ..

# Tests (pytest.ini adds . and backend/ to the import path)
pytest tests/ -v

# API boots; open http://localhost:8000/docs and confirm POST /analyze/{job_id}/run
cd backend && uvicorn main:app --reload --port 8000

# No local paths or secrets in reporting code
grep -r "local_path\|/tmp/\|api_key\|secret" backend/services/reporting/
```

The grep always matches `build_report.py`, because the sanitizer has to list the keys
and path prefixes it strips. Anything outside that file is a real finding:

```bash
grep -rn --include='*.py' "local_path\|/tmp/\|api_key\|secret" backend/services/reporting/ \
  | grep -v '^backend/services/reporting/build_report.py:'
```

## 4. Pre-demo checklist

- [ ] `backend/.env` exists and required keys are filled
- [ ] `pytest tests/ -v` is green
- [ ] `uvicorn main:app --port 8000` boots with no traceback
- [ ] `npm run dev` boots on http://localhost:3000
- [ ] http://localhost:8000/docs shows `POST /analyze/{job_id}/run`
- [ ] No `/tmp/` or local paths in reporting code (section 3 grep)
- [ ] `git status` does not list `backend/.env` or any `.env`
- [ ] One end-to-end run succeeds: submit a post, report saved, report page opens
- [ ] Screenshot the report page as a fallback for the live demo

## 5. Not my job

Ask the owner directly; the reporting layer only consumes these outputs.

| Owner | Scope |
|---|---|
| Abdullah | `retrieve_post`, `extract_media_features`, `build_search_queries`, `search_candidates` |
| Jason | `retrieve_candidate_media`, `verify_and_align`, `select_surrounding_context`, `compare_context_and_caption` |
| Grant | Frontend pages: `frontend/app/page.tsx` (submission), `frontend/app/results/page.tsx`, report page at `/reports/[id]` |

The Abdullah/Jason split above follows pipeline order and still needs confirmation.
The backend links to `{REPORT_BASE_URL}/reports/{report_id}`; the README's `/report/[id]`
must be renamed to match, or the iMessage link will 404.
