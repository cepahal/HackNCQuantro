# Cleanup log — 2026-10-10

Each candidate was checked for (1) importers across `backend/`, `tests/`, `benchmarks/`,
`frontend/`, `backend/services/photon/`, docs and package scripts; (2) changes in the
last five `origin/main` commits (origin/main had not moved past `913f7ff`); and
(3) whether it is a config file.

## Deleted

| File | Reason | Replacement |
|---|---|---|
| `backend/services/_archive/analyze.py` | One-line "archived, empty unreferenced merge scaffold"; no importers | None needed |
| `backend/services/_archive/example_api.py` | Same | None needed |
| `backend/services/_archive/helpers.py` | Same | None needed |
| `backend/services/_archive/scoring.py` | Same | None needed |
| `frontend/lib/displayModel.ts` | Deleted earlier by the frontend agent; types moved | `frontend/lib/api-types.ts` |
| `frontend/AGENTS.md` (untracked) | Written by `next dev` (`node_modules/next/dist/server/lib/generate-agent-files.js`); not project content | None. `next dev` may re-create it; do not stage it |
| `backend/utils/` (untracked, git-ignored) | Contained only a stale `__pycache__/helpers.cpython-314.pyc` with no source | None needed |

`docs/architecture.md` was updated because it stated the `_archive` scaffolds were
retained. `docs/MVP_INTEGRATION_AUDIT.md` still mentions them as a dated audit record.

## Kept

| File | Reason |
|---|---|
| `backend/services/reporting/types.py` | Compatibility re-export of `shared/types.py`, pinned by `tests/test_contract_shapes.py::test_legacy_type_imports_reexport_the_canonical_class`; edited by Jason in `7f9d614` (last five commits) |
| `backend/services/reporting/errors.py` | Compatibility re-export of `shared/errors.py`, pinned by `test_error_imports_reexport_the_canonical_class`; imported by 15 test files including `tests/test_photon_reporting.py`, which was being edited by the Photon webhook work |
| `backend/services/external_apis/post_contract.py`, `search_contract.py` | Abdullah-owned compatibility shims; `tests/test_types_contract.py` and `tests/test_contract_shapes.py` assert they re-export the canonical classes; `docs/retrieve_post.md` documents `post_contract.py` |
| `backend/services/external_apis/snowflake_storage.py` | Abdullah-owned optional adapter with its own tests (`tests/test_snowflake_storage.py`) and documented `SNOWFLAKE_*` settings; not wired into `REPORT_STORE` but not dead |
| `frontend/app/reports/[report_id]/page.tsx` | Redirect for `/reports/{id}`, the URL the backend sends in iMessage summaries |
| `frontend/app/results/page.tsx` | Redirects `/results?report_id=` to `/results/{id}`; a different URL, not a duplicate |
| Root `.env`, `.env.local` | User env files; both git-ignored (`.gitignore` lines 2 and 4) |

No `*.bak`, `*.orig`, `*_old*`, `*_v1*`, `*_deprecated*`, `mock_*.py` or `fake_*.py` files,
no `backend/services/reporting/_stubs.py`, and no API client besides `frontend/lib/api.ts`
calling `fetch` were found. `next build` reports no duplicate routes.

No imports needed repointing.
