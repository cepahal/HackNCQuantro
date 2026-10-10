# Cleanup log — 2026-10-10

Each candidate was checked for importers and references across `backend/`, `tests/`,
`benchmarks/`, `frontend/`, `backend/services/photon/`, docs, scripts, `render.yaml`
and Dockerfiles, and against recent teammate commits. Every deleted file can be
restored from git history: `git show <sha>:<path>`.

## Pass 1 (commit `8633da5`)

| File | Reason | Replacement | Restore from |
|---|---|---|---|
| `backend/services/_archive/{analyze,example_api,helpers,scoring}.py` | One-line empty merge scaffolds; no importers | None needed | `913f7ff` |
| `frontend/lib/displayModel.ts` | Types moved by the frontend work | `frontend/lib/api-types.ts` | `913f7ff` |
| `frontend/AGENTS.md` (untracked) | Written by `next dev`; not project content. `next dev` may re-create it; do not stage it | None | n/a |
| `backend/utils/` (untracked, git-ignored) | Only a stale `__pycache__/helpers.cpython-314.pyc`, no source | None | n/a |

## Pass 2 (after Jason's `2f7643e`; the user authorized removing repeated/unused files)

| File | Reason | Replacement | Restore from |
|---|---|---|---|
| `backend/services/reporting/types.py` | Re-export shim; no production importers | `backend/services/shared/types.py` | `2f7643e` |
| `backend/services/reporting/errors.py` | Re-export shim; no production importers | `backend/services/shared/errors.py` (also re-exported by `backend/services/reporting/__init__.py`) | `2f7643e` |
| `backend/services/external_apis/post_contract.py` | Re-export shim (Abdullah); tests only | `backend/services/shared/types.py` | `2f7643e` |
| `backend/services/external_apis/search_contract.py` | Re-export shim (Abdullah); tests only | `backend/services/shared/types.py` | `2f7643e` |
| `backend/services/external_apis/snowflake_storage.py`, `snowflake_schema.sql` | Abdullah's optional Snowflake adapter; nothing in production imports it, `REPORT_STORE` accepts only `tiger`/`memory`, and no requirement installs the connector | Tiger Data (`tigerdata_storage.py`, `storage/tiger.py`) | `2f7643e` |
| `tests/test_snowflake_storage.py` | Tested only the deleted adapter | None | `2f7643e` |
| `backend/.env.example` | Duplicate of the root template; nothing referenced it | Root `.env.example` (now includes the Photon webhook, Exa and Whisper keys) | `2f7643e` |
| `docs/MVP_INTEGRATION_AUDIT.md` | Point-in-time audit referencing removed files | `docs/INTEGRATION.md`, `docs/RUNBOOK.md`, `docs/PROVIDERS.md`, this log | `2f7643e` |

Test imports in 16 files were repointed to `backend.services.shared.types` /
`backend.services.shared.errors`. Tests that only asserted the shims re-exported the
canonical classes were removed (`test_post_models_match_retrieval_owner_contract`, four
shim rows of `test_legacy_type_imports_reexport_the_canonical_class`);
`test_error_imports_reexport_the_canonical_class` became
`test_reporting_package_reexports_the_canonical_error`. `tests/test_retrieve_post.py` still asserts retrieval never imports Snowflake.

The root `.env.example` dropped keys no code reads: `RETRIEVAL_PROVIDER_API_KEY`,
`SEARCH_PROVIDER_API_KEY`, `TRANSCRIPTION_API_KEY` and `SNOWFLAKE_*`.

## Kept

| File | Reason |
|---|---|
| `backend/services/messaging/delivery_tracker.py` | Requested path; imported by `routes/photon_webhook.py` and `tests/test_photon_webhook.py` |
| `docs/PROVIDERS.md` | Canonical Exa/Whisper provider reference; deploy steps live in `docs/RUNBOOK.md` section 6 and link to it |
| `README.md` | Original project plan; mentions Snowflake as narrative, not configuration |
| `frontend/app/reports/[report_id]/page.tsx` | Redirect for `/reports/{id}`, the URL the backend sends in iMessage summaries |
| `frontend/app/results/page.tsx` | Redirects `/results?report_id=` to `/results/{id}`; a different URL |
| `benchmarks/`, `docs/evidence/`, Photon Node sidecar | In use |
| Root `.env`, `.env.local`, `.secrets/` | User env files; git-ignored |

No tracked `.DS_Store`, `__pycache__`, `.pyc`, `.bak` or `.orig` files, and no duplicate
requirement entries, were found.

## Pass 3 — Grant's frontend (`origin/feature/frontend-work`) becomes the UI

Grant's pages and components replaced the earlier MVP UI in `frontend/`. His mock data
layer was replaced by the canonical client (`lib/api.ts`), types (`lib/api-types.ts`) and
display rules (`lib/display.ts`). Main-side files restore from `25d092d`; Grant's from `b859a0b`.

| File | Reason | Replacement | Restore from |
|---|---|---|---|
| `frontend/lib/fixtures.ts` | Grant's mock dog-foster report; the UI now loads real reports | `GET /reports/{report_id}` via `lib/api.ts` | `b859a0b` |
| `frontend/lib/schema.ts` | Zod report schema duplicating the runtime guard; dropped the `zod` dependency | `assertReport` in `lib/api.ts` | `b859a0b` |
| `frontend/lib/types.ts` | Second copy of the ReportV2 types | `lib/api-types.ts` (`ReportV2`) | `b859a0b` |
| `frontend/components/ShareButton.tsx` | Duplicates Grant's "Copy link" button in the report header | `app/results/page.tsx` | `25d092d` |
| `frontend/components/PhotonBanner.tsx` | No slot in Grant's layout; the number now fills his hero note | `app/page.tsx` (`NEXT_PUBLIC_PHOTON_NUMBER`) | `25d092d` |
| `frontend/components/ReportPage.tsx` | Report loading moved into Grant's results page | `app/results/page.tsx` | `25d092d` |
| `frontend/components/ConfidenceSection.tsx` | Unused after Grant's `ResultCard` instruments replaced it | `components/ResultCard.tsx` + `lib/display.ts` | `25d092d` |

The "Kept" row above for `frontend/app/results/page.tsx` is superseded: it is now Grant's
report page (`/results?id=`, also accepting `?report_id=`). `/reports/{id}` and `/results/{id}`
redirect to it.
