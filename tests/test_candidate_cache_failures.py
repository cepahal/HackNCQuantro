"""Optional cached snapshots must not suppress fresh source verification."""

import importlib
from unittest.mock import Mock

import pytest
import test_run_analysis as fixtures

from backend.services.shared.types import MediaFeatures


@pytest.mark.parametrize("snapshot", [
    {"post_json": {}, "features_json": {}},
    {"post_json": fixtures._post("source", "https://youtu.be/source"),
     "features_json": fixtures._source_features("different")},
])
def test_corrupt_or_mismatched_snapshot_falls_back_to_fresh_retrieval(monkeypatch, snapshot):
    module = importlib.import_module("backend.services.reporting.run_analysis")
    post = fixtures._post("source", "https://youtu.be/source")
    features = fixtures._source_features("source")
    retrieve = Mock(return_value=post)
    extract = Mock(return_value=features)
    store = Mock()
    store.get_cached_source.return_value = snapshot
    monkeypatch.setattr(module, "retrieve_candidate_media", retrieve)
    monkeypatch.setattr(module, "extract_media_features", extract)
    result = module._assess_candidate(
        {"candidate_id": "candidate", "url": post["post_url"]},
        MediaFeatures.model_validate(fixtures.CLIP_FEATURES), set(), store=store,
    )
    retrieve.assert_called_once()
    extract.assert_called_once()
    assert any("fresh retrieval" in warning for warning in result.features.warnings)
    assert result.features.media_id == post["media"]["media_id"]
