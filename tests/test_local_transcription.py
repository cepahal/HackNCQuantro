from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.services.processing import preprocessing as p
from backend.services.shared.errors import QuantroError
from backend.services.shared.types import MediaFeatures
from backend.services.specialist import transcription as t


def test_provider_defaults_and_rejects_unknown(monkeypatch):
    monkeypatch.setattr(t, 'load_settings', lambda: None)
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', '')
    assert t.provider() == 'whisper'
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'whisper')
    assert t.provider() == 'whisper'
    monkeypatch.setenv('TRANSCRIPTION_PROVIDER', 'unknown')
    with pytest.raises(ValueError):
        t.provider()


def test_local_provider_preserves_spoken_segments(monkeypatch, tmp_path):
    path = tmp_path / "local.wav"
    path.write_bytes(b"provider test")
    model = Mock()
    model.transcribe.return_value = (iter([
        SimpleNamespace(start=1.2, end=3.4, text=' Do not cancel it. '),
        SimpleNamespace(start=3.4, end=4.0, text=' '),
    ]), SimpleNamespace(language='en'))
    monkeypatch.setattr(t, '_model', lambda name: model)
    result = t.transcribe(path)
    assert result == {'language': 'en', 'sentences': [
        {'start_seconds': 1.2, 'end_seconds': 3.4, 'text': 'Do not cancel it.', 'words': []}]}
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


@pytest.mark.parametrize('word_end, expected_count', [(2.0, 1), (1.2, 0)])
def test_local_word_timing_preserved_or_discarded(monkeypatch, tmp_path, word_end, expected_count):
    path = tmp_path / "local.wav"
    path.write_bytes(str(word_end).encode())
    model = Mock()
    model.transcribe.return_value = (iter([SimpleNamespace(
        start=1.2, end=2.0, text=' Hello.',
        words=[SimpleNamespace(start=1.2, end=word_end, word=' Hello.')],
    )]), SimpleNamespace(language='en'))
    monkeypatch.setattr(t, '_model', lambda name: model)
    words = t.transcribe(path)['sentences'][0]['words']
    assert len(words) == expected_count
    if words:
        assert words == [{'text': 'Hello.', 'start_seconds': 1.2, 'end_seconds': 2.0}]


@pytest.mark.parametrize('words', [
    [{'text':'Keep it.', 'start_seconds':0.73, 'end_seconds':2.0}],
    [{'text':'Keep', 'start_seconds':1.0, 'end_seconds':1.6}, {'text':'it.', 'start_seconds':1.5, 'end_seconds':2.0}],
    [{'text':'Keep', 'start_seconds':1.0, 'end_seconds':1.6}],
    [{'text':'invented', 'start_seconds':1.0, 'end_seconds':2.0}],
    [{'text':'Keep it.', 'start_seconds':float('nan'), 'end_seconds':2.0}],
    [{'text':'Keep it.', 'start_seconds':2.0, 'end_seconds':2.0}],
])
def test_unreliable_optional_words_preserve_sentence_and_disclose_fallback(words):
    warnings=[]
    data={'language':'en','sentences':[{'text':'Keep it.', 'start_seconds':1.0, 'end_seconds':2.0,'words':words}]}
    result=p._sentences(data,60,120,'source',1,warnings=warnings)
    assert result == [{'sentence_id':'source_sentence_0001','start_seconds':61.0,'end_seconds':62.0,'text':'Keep it.','speaker_id':None}]
    assert len(warnings)==1 and 'word timings were discarded' in warnings[0]


def test_invalid_sentence_is_still_rejected():
    data={'language':'en','sentences':[{'text':'Keep it.', 'start_seconds':-1, 'end_seconds':2,'words':[]}]}
    with pytest.raises(ValueError):p._sentences(data,0,60,'source',1,warnings=[])


def test_transcript_cache_uses_content_and_model_and_isolation(tmp_path, monkeypatch):
    t._TRANSCRIPTS.clear()
    calls = []
    def decode(path, name):
        calls.append(name)
        return {'language': 'en', 'sentences': [{'text': 'Original'}]}
    monkeypatch.setattr(t, '_transcribe_uncached', decode)
    monkeypatch.setenv('WHISPER_MODEL', 'model-a')
    a, b = tmp_path / 'a.wav', tmp_path / 'b.wav'
    a.write_bytes(b'same audio')
    b.write_bytes(b'same audio')
    t.transcribe(a)['sentences'][0]['text'] = 'mutated'
    assert t.transcribe(b)['sentences'][0]['text'] == 'Original'
    assert calls == ['model-a']
    b.write_bytes(b'changed audio')
    t.transcribe(b)
    monkeypatch.setenv('WHISPER_MODEL', 'model-b')
    t.transcribe(b)
    assert calls == ['model-a', 'model-a', 'model-b']
    monkeypatch.setattr(t, '_CACHE_LIMIT', 2)
    a.write_bytes(b'another')
    t.transcribe(a)
    assert len(t._TRANSCRIPTS) == 2
    t._TRANSCRIPTS.clear()


def test_failed_transcription_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / 'failure.wav'
    path.write_bytes(b'failed audio')
    decode = Mock(side_effect=RuntimeError('failed'))
    monkeypatch.setattr(t, '_transcribe_uncached', decode)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            t.transcribe(path)
    assert decode.call_count == 2
