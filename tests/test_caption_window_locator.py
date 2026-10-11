"""Caption starts locate native-video checks; they never verify a source alone."""

from unittest.mock import Mock

import pytest
from test_remote_source_analysis import OTHER, SOURCE, SPEECH, make_report, row

from backend.services.external_apis.youtube_transcript import TimedCaptionCue
from backend.services.reporting import remote_source_analysis as remote
from backend.services.shared.errors import ErrorCode, QuantroError, Stage


def cue(start, text):
    return TimedCaptionCue(start_seconds=float(start), text=text)


def test_fingerprint_crosses_caption_boundaries_and_locates_beyond_first_minute():
    cues = [cue(0, 'An unrelated opening and introductions.'),
            cue(823, 'If the employment numbers were accurate'),
            cue(831, 'we would reconsider the public program.')]
    assert remote._caption_window_start(make_report().submitted.transcript, cues) == 808


def test_repeated_exact_caption_passage_cannot_choose_a_location():
    cues = [cue(120, SPEECH), cue(900, SPEECH)]
    assert remote._caption_window_start(make_report().submitted.transcript, cues) is None


@pytest.mark.parametrize('cues', [[], [cue(500, OTHER)]])
def test_absent_or_unrelated_captions_cannot_locate_a_speech(cues):
    assert remote._caption_window_start(make_report().submitted.transcript, cues) is None


def test_caption_start_is_not_reinterpreted_as_spoken_end_or_exact_word_time():
    cues = [cue(4.75, SPEECH)]
    assert remote._caption_window_start(make_report().submitted.transcript, cues) == 0
    assert 'end_seconds' not in TimedCaptionCue.model_fields


def shifted_source(url, offset, *, text=SPEECH):
    result = make_report(url, text=text, source_context=True)
    if offset:
        result.submitted.post.post_url += f'&t={offset}'
        result.submitted.post.media.media_id += f':window:{offset}'
    for sentence in result.submitted.transcript.sentences:
        sentence.start_seconds += offset
        sentence.end_seconds += offset
    for interval in result.submitted.transcript.processed_ranges:
        interval.start_seconds += offset
        interval.end_seconds += offset
    return result


def test_caption_locator_targets_one_later_native_window_and_preserves_absolute_context(monkeypatch):
    captions = Mock(return_value=[cue(823, SPEECH)])
    native = Mock(side_effect=lambda job, url, **kwargs: shifted_source(url, kwargs.get('window_start_seconds', 0)))
    monkeypatch.setattr(remote, 'fetch_youtube_transcript', captions)
    monkeypatch.setattr(remote, 'build_remote_youtube_report', native)
    monkeypatch.setattr(remote, 'compare_transcript_windows', lambda *args: remote._not_assessed(None))
    warnings = []
    result = remote.assess_remote_sources(make_report(), [row()], warnings, lambda: None)
    captions.assert_called_once_with(SOURCE)
    native.assert_called_once_with(result.job_id, SOURCE, window_start_seconds=808)
    assert result.outcome == 'matched'
    assert result.source.post.post_url == SOURCE + '&t=808'
    assert result.alignment.matches[0].source_start_seconds == 813
    assert all(sentence.start_seconds >= 808 for sentence in result.context.windows[0].sentences)
    assert result.source.post.media.duration_seconds is None
    assert result.alignment.match_quality.clip_coverage_ratio is None
    assert any('locator hints' in warning for warning in warnings)


def test_captions_alone_never_verify_a_recording_when_native_speech_differs(monkeypatch):
    monkeypatch.setattr(remote, 'fetch_youtube_transcript', lambda url: [cue(823, SPEECH)])
    native = Mock(side_effect=lambda job, url, **kwargs: shifted_source(url, kwargs['window_start_seconds'], text=OTHER))
    comparison = Mock(side_effect=AssertionError('Unverified speech must not reach comparison.'))
    monkeypatch.setattr(remote, 'build_remote_youtube_report', native)
    monkeypatch.setattr(remote, 'compare_transcript_windows', comparison)
    result = remote.assess_remote_sources(make_report(), [row()], [], lambda: None)
    assert result.outcome == 'no_verified_match'
    assert result.source is result.alignment is result.context is result.comparison is None
    comparison.assert_not_called()
    native.assert_called_once()


def test_caption_provider_failure_retains_native_fallback_and_redacts_diagnostics(monkeypatch):
    failure = Mock(side_effect=QuantroError(ErrorCode.SEARCH_FAILED, Stage.SEARCH_CANDIDATES,
                                          'PRIVATE_PROVIDER_DIAGNOSTIC', retryable=True))
    native = Mock(return_value=make_report(SOURCE, source_context=True))
    monkeypatch.setattr(remote, 'fetch_youtube_transcript', failure)
    monkeypatch.setattr(remote, 'build_remote_youtube_report', native)
    monkeypatch.setattr(remote, 'compare_transcript_windows', lambda *args: remote._not_assessed(None))
    result = remote.assess_remote_sources(make_report(), [row()], [], lambda: None)
    assert result.outcome == 'matched'
    native.assert_called_once_with(result.job_id, SOURCE)
    assert 'Timed captions were unavailable' in result.model_dump_json()
    assert 'PRIVATE_PROVIDER_DIAGNOSTIC' not in result.model_dump_json()


def test_explicit_candidate_offset_does_not_issue_a_caption_lookup(monkeypatch):
    captions = Mock(side_effect=AssertionError('An explicit offset already locates the window.'))
    native = Mock(side_effect=lambda job, url, **kwargs: shifted_source(url, kwargs['window_start_seconds']))
    monkeypatch.setattr(remote, 'fetch_youtube_transcript', captions)
    monkeypatch.setattr(remote, 'build_remote_youtube_report', native)
    monkeypatch.setattr(remote, 'compare_transcript_windows', lambda *args: remote._not_assessed(None))
    result = remote.assess_remote_sources(make_report(), [row(SOURCE + '&t=125')], [], lambda: None)
    assert result.outcome == 'matched'
    captions.assert_not_called()
    native.assert_called_once_with(result.job_id, SOURCE, window_start_seconds=125)
