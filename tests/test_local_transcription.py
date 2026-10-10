from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.processing import preprocessing as p
from backend.services.specialist import transcription as t
from backend.services.reporting.errors import QuantroError
from backend.services.reporting.types import MediaFeatures


def test_provider_defaults_and_rejects_unknown(monkeypatch):
    monkeypatch.setattr(t, 'load_settings', lambda: None)
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', '')
    assert t.provider() == 'gemini'
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'whisper')
    assert t.provider() == 'whisper'
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'unknown')
    with pytest.raises(ValueError):
        t.provider()


def test_local_provider_preserves_spoken_segments(monkeypatch):
    model = Mock()
    model.transcribe.return_value = (iter([
        SimpleNamespace(start=1.2, end=3.4, text=' Do not cancel it. '),
        SimpleNamespace(start=3.4, end=4.0, text=' '),
    ]), SimpleNamespace(language='en'))
    monkeypatch.setattr(t, '_model', lambda name: model)
    result = t.transcribe('local.wav')
    assert result == {'language': 'en', 'sentences': [
        {'start_seconds': 1.2, 'end_seconds': 3.4, 'text': 'Do not cancel it.'}]}
    assert model.transcribe.call_args.kwargs['condition_on_previous_text'] is False


def test_whisper_extraction_needs_no_gemini_and_offsets_ranges(tmp_path, monkeypatch):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'test')
    monkeypatch.setattr(p, '_probe', lambda path: (30.0, True, False))
    monkeypatch.setattr(p, '_run', lambda args: b'')
    monkeypatch.setattr(t, 'provider', lambda: 'whisper')
    monkeypatch.setattr(t, 'transcribe', lambda path: {'language': 'en', 'sentences': [
        {'start_seconds': 1.0, 'end_seconds': 2.0, 'text': 'Keep these words.'}]})
    client = Mock(side_effect=AssertionError('Local speech must not require an API key'))
    monkeypatch.setattr(p, 'client_and_model', client)
    result = p.extract_media_features({'media_id': 'clip', 'local_path': str(path)}, {
        'transcript_scope': 'ranges', 'transcript_ranges': [{'start_seconds': 10, 'end_seconds': 20}],
        'extract_frames': False, 'extract_visual_text': False})
    assert result['transcript']['sentences'][0]['start_seconds'] == 11
    assert result['transcript']['status'] == 'partial'
    MediaFeatures.model_validate(result)
    client.assert_not_called()


def test_local_failure_does_not_fall_back_to_network(tmp_path, monkeypatch):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'test')
    monkeypatch.setattr(p, '_probe', lambda path: (5.0, True, False))
    monkeypatch.setattr(p, '_run', lambda args: b'')
    monkeypatch.setattr(t, 'provider', lambda: 'whisper')
    monkeypatch.setattr(t, 'transcribe', Mock(side_effect=RuntimeError('private details')))
    with pytest.raises(QuantroError) as error:
        p.extract_media_features({'media_id': 'clip', 'local_path': str(path)}, {
            'extract_frames': False, 'extract_visual_text': False})
    assert error.value.code.value == 'TRANSCRIPTION_FAILED'
    assert 'private details' not in str(error.value)
