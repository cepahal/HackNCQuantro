"""Small checks locking the shared public contracts and startup registry."""

import importlib
from typing import get_args

import pytest
from pydantic import ValidationError

from backend.services.shared import types as shared
from backend.services.shared.errors import QuantroError

CONTRACT_FIELDS = [
    ("retrieve_post", shared.Post, {"post_id", "candidate_id", "post_url", "platform", "title", "caption", "published_at", "author", "engagement", "media", "retrieval"}),
    ("extract_media_features", shared.MediaFeatures, {"media_id", "duration_seconds", "language", "transcript", "frames", "visual_text", "warnings"}),
    ("build_search_queries", shared.Queries, {"queries", "warnings"}),
    ("search_candidates", shared.CandidateList, {"candidates", "warnings"}),
    ("retrieve_candidate_media", shared.Post, {"post_id", "candidate_id", "post_url", "platform", "title", "caption", "published_at", "author", "engagement", "media", "retrieval"}),
    ("verify_and_align", shared.Alignment, {"status", "clip_media_id", "source_media_id", "matches", "match_quality", "edit_analysis", "limitations"}),
    ("select_surrounding_context", shared.Context, {"source_media_id", "windows", "limitations"}),
    ("compare_context_and_caption", shared.Comparison, {"context_assessment", "caption_assessment", "assessment_confidence", "summary", "findings", "limitations"}),
    ("build_report", shared.ReportV2, {"schema_version", "job_id", "outcome", "submitted", "source", "alignment", "context", "comparison", "summary", "limitations"}),
    ("save_report_and_complete_job", shared.JobCompletionResult, {"job_id", "status", "report_id", "report_url", "completed_at"}),
    ("display_report", shared.DisplayModel, {"source_match", "clip_coverage", "context_assessment", "confidence_note", "no_source_message", "context_unavailable_message", "submitted_panel", "source_panel", "poster_info", "matches", "edit_analysis", "findings", "limitations"}),
    ("send_summary", shared.DeliveryResult, {"conversation_id", "message_id", "status"}),
    ("run_analysis", shared.AnalysisResult, {"job_id", "outcome", "report_id", "report_url", "delivery_status", "delivery_warning", "warnings"}),
]


@pytest.mark.parametrize("name,model,fields", CONTRACT_FIELDS, ids=[row[0] for row in CONTRACT_FIELDS])
def test_contract_models_have_exact_fields(name, model, fields):
    assert set(model.model_fields) == fields


INVALID_CALLS = [
    ("backend.services.external_apis.retrieve_post", "retrieve_post", (None,)),
    ("backend.services.processing.preprocessing", "extract_media_features", ({}, {})),
    ("backend.services.external_apis.build_search_queries", "build_search_queries", ({}, {})),
    ("backend.services.external_apis.search_candidates", "search_candidates", (None, 0)),
    ("backend.services.external_apis.retrieve_candidate_media", "retrieve_candidate_media", ({},)),
    ("backend.services.specialist.alignment", "verify_and_align", ({}, {})),
    ("backend.services.specialist.context", "select_surrounding_context", ({}, {}, {})),
    ("backend.services.specialist.comparison", "compare_context_and_caption", ({}, {}, None)),
    ("backend.services.reporting.build_report", "build_report", ("", {}, {}, None, None, None, None, None)),
    ("backend.services.reporting.save_report", "save_report_and_complete_job", ("", {})),
    ("backend.services.reporting.display_report", "display_report", ({},)),
    ("backend.services.reporting.send_summary", "send_summary", ("", "", "")),
]


@pytest.mark.parametrize("module_path,name,args", INVALID_CALLS, ids=[row[1] for row in INVALID_CALLS])
def test_invalid_input_raises_the_shared_error(module_path, name, args):
    function = getattr(importlib.import_module(module_path), name)
    with pytest.raises(QuantroError):
        function(*args)


def _public_report():
    return {
        "schema_version": "2.1", "job_id": "job_contract", "outcome": "no_verified_match",
        "submitted": {
            "post": {
                "post_id": "post_contract", "candidate_id": None, "post_url": "https://example.com/clip.mp4",
                "platform": "direct", "title": None, "caption": None, "published_at": None,
                "author": {"followers_count": None}, "engagement": {},
                "media": {"media_id": "media_contract", "mime_type": "video/mp4", "playback_url": None},
                "retrieval": {"retrieved_at": "2026-10-10T17:00:00Z", "provider": "fixture", "metadata_source_urls": [], "unavailable_fields": [], "warnings": []},
            },
            "transcript": {"status": "unavailable", "processed_ranges": [], "sentences": []},
            "frames": [], "visual_text": [],
        },
        "source": None, "alignment": None, "context": None, "comparison": None,
        "summary": None, "limitations": ["No verified source was found."],
    }


def test_report_schema_is_literal_2_1():
    assert get_args(shared.ReportV2.model_fields["schema_version"].annotation) == ("2.1",)
    assert shared.ReportV2.model_fields["schema_version"].default == "2.1"
    with pytest.raises(ValidationError):
        shared.ReportV2.model_validate({**_public_report(), "schema_version": "2.0"})


def test_unknown_follower_count_stays_null_in_json():
    report = shared.ReportV2.model_validate(_public_report())
    restored = shared.ReportV2.model_validate_json(report.model_dump_json())
    assert restored.submitted.post.author.followers_count is None
    assert '"followers_count":null' in restored.model_dump_json()


def test_return_boundary_validates_legacy_json_native_shape():
    from backend.services.shared.contracts import validate_contract_result
    from backend.services.shared.errors import Stage

    result = validate_contract_result(shared.Queries, {"queries": [], "warnings": []}, Stage.BUILD_SEARCH_QUERIES, json_native=True)
    assert isinstance(result, dict)
    assert result == {"queries": [], "warnings": []}
    with pytest.raises(QuantroError):
        validate_contract_result(shared.Queries, {"queries": "wrong type", "warnings": []}, Stage.BUILD_SEARCH_QUERIES, json_native=True)


@pytest.mark.parametrize("module_path,name", [
    ("backend.services.reporting.types", "Post"),
    ("backend.services.external_apis.post_contract", "Post"),
    ("backend.services.external_apis.post_contract", "MediaAsset"),
    ("backend.services.external_apis.search_contract", "Queries"),
    ("backend.services.specialist.comparison", "Comparison"),
    ("backend.services.reporting.display_report", "DisplayModel"),
    ("backend.services.reporting.run_analysis", "AnalysisResult"),
    ("backend.services.pipeline.run_ingest", "IngestResult"),
])
def test_legacy_type_imports_reexport_the_canonical_class(module_path, name):
    assert getattr(importlib.import_module(module_path), name) is getattr(shared, name)


def test_error_imports_reexport_the_canonical_class():
    from backend.services.reporting.errors import QuantroError as legacy_error

    assert legacy_error is QuantroError


def test_startup_checks_every_contract_function():
    from backend.main import _CONTRACT_FUNCTION_PATHS, validate_contract_functions

    assert set(_CONTRACT_FUNCTION_PATHS) == {row[0] for row in CONTRACT_FIELDS}
    validate_contract_functions()


def test_startup_missing_function_names_the_expected_import(monkeypatch):
    from backend.main import validate_contract_functions

    module = importlib.import_module("backend.services.reporting.build_report")
    monkeypatch.delattr(module, "build_report")
    with pytest.raises(RuntimeError, match=r"build_report.*backend\.services\.reporting\.build_report\.build_report"):
        validate_contract_functions()


def test_startup_unimportable_function_names_the_expected_import(monkeypatch):
    from backend import main

    original = main.import_module

    def unavailable(module_path):
        if module_path == "backend.services.external_apis.retrieve_post":
            raise ImportError("Missing dependency")
        return original(module_path)

    monkeypatch.setattr(main, "import_module", unavailable)
    with pytest.raises(RuntimeError, match=r"retrieve_post.*backend\.services\.external_apis\.retrieve_post\.retrieve_post"):
        main.validate_contract_functions()
