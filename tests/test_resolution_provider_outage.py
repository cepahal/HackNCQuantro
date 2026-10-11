"""Offline source preservation when optional language assessment is unavailable."""
from unittest.mock import Mock

import pytest
import test_resolution_pipeline as pipeline_fixtures
from test_resolution_pipeline import CLIP, SOURCE, WORDS

from backend.services.external_apis.tigerdata_storage import StorageError
from backend.services.resolution import analysis, run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage


@pytest.fixture
def store(tmp_path):
    yield from pipeline_fixtures.store.__wrapped__(tmp_path)


@pytest.fixture
def wired(monkeypatch):
    return pipeline_fixtures.wired.__wrapped__(monkeypatch)


@pytest.mark.parametrize('message', [
    'The analysis provider is not configured.',
    'The analysis provider is temporarily unavailable.',
    'The context assessment did not pass its evidence checks.',
])
@pytest.mark.parametrize('caption', [None, 'Attributed claim'])
def test_language_failure_preserves_source_and_saved_report(store, wired, monkeypatch, message, caption):
    original = run.resolve_media.side_effect
    async def resolve(url, **kwargs):
        post, features = await original(url, **kwargs)
        if not kwargs.get('candidate_id'):
            post.caption = caption
        return post, features
    monkeypatch.setattr(run.resolve_media, 'side_effect', resolve)
    monkeypatch.setenv('CONTEXT_VIDEO_ENABLED', 'false')
    monkeypatch.setattr(run, 'compare_clip', Mock(side_effect=QuantroError(
        ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION, message,
    )))
    interpretation = Mock(side_effect=AssertionError('No extra interpretation call'))
    monkeypatch.setattr(run, 'interpret_clip', interpretation)
    result = run.run_analysis('job_language_outage', CLIP, store=store)
    report = store.get_report(result.report_id)
    assert result.outcome.value == 'matched'
    assert report['source']['post']['post_url'] == SOURCE
    assert report['alignment']['status'] == 'verified'
    assert report['context']['windows']
    comparison = report['comparison']
    assert comparison['context_assessment'] == 'not_assessed'
    assert comparison['caption_assessment'] == ('not_assessed' if caption else 'not_provided')
    assert comparison['assessment_confidence']['level'] == 'not_assessed'
    assert comparison['findings'] == []
    assert WORDS in report['summary']
    assert 'not a conclusion' in report['summary']
    assert report.get('context_video') is None
    assert any(e['data'].get('reason') == 'language_assessment_unavailable'
               for e in store.get_diagnostics('job_language_outage'))
    interpretation.assert_not_called()


def test_actual_missing_key_keeps_verified_source(store, wired, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv('CONTEXT_VIDEO_ENABLED', 'false')
    monkeypatch.setattr(run, 'compare_clip', analysis.compare_clip)
    result = run.run_analysis('job_actual_missing_key', CLIP, store=store)
    assert result.outcome.value == 'matched'
    assert store.get_report(result.report_id)['comparison']['context_assessment'] == 'not_assessed'


@pytest.mark.parametrize('error', [
    QuantroError(ErrorCode.STORAGE_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION),
    QuantroError(ErrorCode.PROCESSING_FAILED, Stage.RETRIEVE_CANDIDATE_MEDIA),
    RuntimeError('unrelated internal failure'),
    StorageError('unrelated storage failure'),
])
def test_unrelated_failures_are_not_masked(store, wired, monkeypatch, error):
    monkeypatch.setenv('CONTEXT_VIDEO_ENABLED', 'false')
    monkeypatch.setattr(run, 'compare_clip', Mock(side_effect=error))
    with pytest.raises(QuantroError):
        run.run_analysis('job_unrelated_failure', CLIP, store=store)
    assert store.get_job_reports('job_unrelated_failure') == []
