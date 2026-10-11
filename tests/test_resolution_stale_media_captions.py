"""Independent YouTube captions must survive stale local media references."""
import json
from unittest.mock import AsyncMock, Mock

import pytest
from youtube_transcript_api import RequestBlocked

from backend.services.resolution import transcribe

VIDEO_ID = 'R0ukW4AzcSs'
URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'
TEXT = 'The independently recorded words remain available in timed captions.'


@pytest.mark.asyncio
async def test_missing_local_file_preserves_existing_timed_caption_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, '_CAPTION_CACHE_DIR', tmp_path)
    cached = {'transcript': TEXT, 'duration': 12.0, 'source': 'youtube_captions',
              'segments': [{'text': TEXT, 'start': 2.0, 'end': 7.0}]}
    (tmp_path / f'{VIDEO_ID}.json').write_text(json.dumps(cached))
    native = Mock(side_effect=AssertionError('Cached evidence must avoid provider lookup'))
    monkeypatch.setattr(transcribe, '_fetch_youtube_captions_uncached', native)
    audio = AsyncMock(side_effect=AssertionError('No download or ASR'))
    monkeypatch.setattr(transcribe, 'transcribe_youtube_audio', audio)
    result = await transcribe.get_video_transcript(URL, str(tmp_path / 'removed.mp4'))
    assert result['source'] == 'youtube_captions'
    assert result['segments'] == cached['segments']
    assert result['transcript'] == TEXT
    native.assert_not_called()
    audio.assert_not_called()


@pytest.mark.asyncio
async def test_negative_native_cache_still_uses_independent_hosted_captions(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, '_CAPTION_CACHE_DIR', tmp_path)
    (tmp_path / f'{VIDEO_ID}.json').write_text(json.dumps({'no_captions': 'TranscriptsDisabled'}))
    hosted = {'transcript': TEXT, 'duration': None, 'duration_known': False,
              'coverage': 'partial', 'source': 'serpapi_captions',
              'timing_basis': 'caption_cue_windows',
              'segments': [{'text': TEXT, 'start': 2.0, 'end': 7.0}]}
    fetch = Mock(return_value=hosted)
    monkeypatch.setattr(transcribe, '_fetch_hosted_captions', fetch)
    result = await transcribe.get_video_transcript(URL, str(tmp_path / 'removed.mp4'))
    assert result['source'] == 'serpapi_captions'
    assert result['segments'] == hosted['segments']
    assert result['duration'] is None
    assert result['duration_known'] is False
    fetch.assert_called_once_with(VIDEO_ID, known_duration=None)
    assert not json.loads((tmp_path / f'{VIDEO_ID}.json').read_text()).get('no_captions')


@pytest.mark.asyncio
async def test_missing_file_without_caption_evidence_remains_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, '_fetch_youtube_captions_sync', Mock(side_effect=RequestBlocked(VIDEO_ID)))
    audio = AsyncMock(side_effect=AssertionError('Missing supplied file must not add a retry'))
    monkeypatch.setattr(transcribe, 'transcribe_youtube_audio', audio)
    with pytest.raises(transcribe.TranscriptionError) as error:
        await transcribe.get_video_transcript(URL, str(tmp_path / 'removed.mp4'))
    assert error.value.code == 'file_not_found'
    audio.assert_not_called()


@pytest.mark.asyncio
async def test_non_youtube_missing_file_keeps_original_failure(tmp_path, monkeypatch):
    captions = Mock(side_effect=AssertionError('Not a YouTube recording'))
    monkeypatch.setattr(transcribe, '_fetch_youtube_captions_sync', captions)
    with pytest.raises(transcribe.TranscriptionError) as error:
        await transcribe.get_video_transcript('https://www.instagram.com/reel/ABCdef/', str(tmp_path / 'removed.mp4'))
    assert error.value.code == 'file_not_found'
    captions.assert_not_called()
