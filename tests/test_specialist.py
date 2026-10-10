from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.services.processing import preprocessing as p
from backend.services.reporting.errors import QuantroError
from backend.services.specialist import comparison as c
from backend.services.specialist.alignment import verify_and_align
from backend.services.specialist.context import select_surrounding_context


def _gemini_features(media, options=None):
    """Exercise the explicit legacy Gemini adapter and serialize at its caller boundary."""
    settings = {"provider": "gemini", **(options or {})} if options is None or isinstance(options, dict) else options
    return p.extract_media_features(media, settings).model_dump(mode="json")


TEXT = 'If those numbers were accurate, I would recommend canceling the program.'
FOLLOWUP = 'But those numbers are not accurate, so that is not my recommendation.'


def features(identity, texts, duration=None):
    duration = duration or len(texts) * 8.0
    return {
        'media_id': identity, 'duration_seconds': duration, 'language': 'en',
        'transcript': {
            'status': 'complete', 'processed_ranges': [{'start_seconds': 0.0, 'end_seconds': duration}],
            'sentences': [{'sentence_id': f'{identity}_{i}', 'start_seconds': i * 8.0,
                           'end_seconds': (i + 1) * 8.0, 'text': text, 'speaker_id': None}
                          for i, text in enumerate(texts)],
        }, 'frames': [], 'visual_text': [], 'warnings': [],
    }


@pytest.fixture
def clip():
    return features('clip', [TEXT])


@pytest.fixture
def source():
    return features('source', ['Would you cancel the program?', TEXT, FOLLOWUP])


@pytest.fixture
def context(clip, source):
    return select_surrounding_context(verify_and_align(clip, source), source)


@pytest.fixture
def comparison():
    return {
        'context_assessment': 'material_context_missing', 'caption_assessment': 'contradicted_by_context',
        'assessment_confidence': {'level': 'moderate', 'method': 'context_evidence_rules_v1',
                                  'basis': ['The speaker rejects the recommendation.'], 'limitations': []},
        'summary': 'The excerpt omits the rejection of the recommendation.',
        'findings': [{'finding_id': 'finding_001', 'type': 'omitted_qualification',
                      'explanation': 'The source rejects the recommendation.',
                      'evidence': [{'media_id': 'source', 'sentence_ids': ['source_2'],
                                    'start_seconds': 16.0, 'end_seconds': 24.0, 'quote': FOLLOWUP}]}],
        'limitations': [],
    }


def test_contract_match(clip, source):
    result = verify_and_align(clip, source)
    assert result['status'] == 'verified'
    assert result['matches'][0]['source_start_seconds'] == 8
    assert result['match_quality']['clip_coverage_ratio'] == 1
    assert result['match_quality']['confidence']['level'] == 'moderate'
    assert result['edit_analysis']['counts']['confirmed_deleted_segments'] is None


def test_repeated_source_passage_is_not_verified(clip):
    result = verify_and_align(clip, features('source', [TEXT, TEXT]))
    assert result['status'] == 'no_verified_match'
    assert result['match_quality']['clip_coverage_ratio'] is None


def test_unrelated_source_rejected(clip):
    result = verify_and_align(clip, features('source', ['Marine biologists discovered unusual coral near a remote island yesterday.']))
    assert result['status'] == 'no_verified_match'
    assert result['matches'] == []


def test_short_generic_phrase_rejected():
    assert verify_and_align(features('clip', ['Thank you very much.']), features('source', ['Thank you very much.']))['status'] == 'no_verified_match'


def test_unavailable_is_not_assessed(clip, source):
    source['transcript'] = {'status': 'unavailable', 'processed_ranges': [], 'sentences': []}
    assert verify_and_align(clip, source)['status'] == 'not_assessed'


def test_negation_not_silently_ignored(clip):
    changed = TEXT.replace('would recommend', 'would not recommend')
    assert verify_and_align(clip, features('source', [changed]))['status'] == 'no_verified_match'


def test_sentence_segmentation_preserves_source_bounds(clip):
    source = features('source', ['If those numbers were accurate,', 'I would recommend canceling the program.'])
    result = verify_and_align(clip, source)
    assert result['status'] == 'verified'
    assert result['matches'][0]['source_end_seconds'] == 16
    assert result['matches'][0]['evidence'][0]['source_refs'] == ['source_0', 'source_1']


def test_internal_gap_has_no_confirmed_edit_count():
    second = 'The committee published revised quarterly estimates following an independent audit.'
    clip = features('clip', [TEXT, second])
    source = features('source', [TEXT, FOLLOWUP, second])
    result = verify_and_align(clip, source)
    assert len(result['matches']) == 2
    assert result['edit_analysis']['source_gaps'][0]['start_seconds'] == 8
    assert result['edit_analysis']['source_gaps'][0]['end_seconds'] == 16
    assert result['edit_analysis']['counts']['confirmed_deleted_segments'] is None


def test_overlapping_clip_matches_do_not_double_count():
    second = 'The committee published revised quarterly estimates following an independent audit.'
    clip = features('clip', [TEXT, second])
    clip['transcript']['sentences'][1]['start_seconds'] = 4
    source = features('source', [TEXT, FOLLOWUP, second])
    result = verify_and_align(clip, source)
    assert result['match_quality']['matched_clip_seconds'] == 16
    assert result['match_quality']['clip_coverage_ratio'] == 1


def test_invalid_features_raise_normalized_error(clip, source):
    clip['duration_seconds'] = float('nan')
    with pytest.raises(QuantroError):
        verify_and_align(clip, source)


def test_context_preserves_extraction_warnings(clip, source):
    source['warnings'] = ['Approximate transcription timestamps.']
    result = select_surrounding_context(verify_and_align(clip, source), source)
    assert 'Approximate transcription timestamps.' in result['limitations']


def test_valid_comparison_caps_confidence(clip, context, comparison):
    comparison['assessment_confidence']['level'] = 'high'
    result = c._validate_result(comparison, clip, context, 'Cancel it')
    assert result['assessment_confidence']['level'] == 'moderate'
    assert result['caption_assessment'] == 'contradicted_by_context'


@pytest.mark.parametrize('field,value', [('quote', 'Invented quotation'), ('sentence_ids', ['absent']), ('media_id', 'unknown'), ('start_seconds', 17.0)])
def test_fabricated_evidence_rejected(clip, context, comparison, field, value):
    comparison['findings'][0]['evidence'][0][field] = value
    with pytest.raises((KeyError, ValueError)):
        c._validate_result(comparison, clip, context, 'Cancel it')


def test_missing_caption_is_not_provided(clip, context, comparison):
    assert c._validate_result(comparison, clip, context, None)['caption_assessment'] == 'not_provided'


def test_partial_context_forces_uncertain(clip, context, comparison):
    context['windows'][0]['coverage'] = 'partial'
    result = c._validate_result(comparison, clip, context, 'Cancel it')
    assert result['context_assessment'] == 'uncertain'
    assert result['assessment_confidence']['level'] == 'low'


def test_empty_context_does_not_call_provider(clip, monkeypatch):
    create = Mock(side_effect=AssertionError('No provider expected'))
    monkeypatch.setattr(c, 'client_and_model', create)
    result = c.compare_context_and_caption(clip, {'source_media_id': 'source', 'windows': []}, None)
    assert result['context_assessment'] == 'not_assessed'
    create.assert_not_called()


def test_comparison_provider_boundary(clip, context, comparison, monkeypatch):
    client = Mock()
    monkeypatch.setattr(c, 'client_and_model', lambda stage: (client, 'test-model'))
    monkeypatch.setattr(c, 'generate_json', lambda *args: comparison)
    result = c.compare_context_and_caption(clip, context, 'Cancel it')
    assert result['context_assessment'] == 'material_context_missing'
    client.close.assert_called_once()


def test_no_findings_cannot_support_definitive_assessment(clip, context, comparison):
    comparison['findings'] = []
    result = c._validate_result(comparison, clip, context, 'Cancel it')
    assert result['context_assessment'] == 'uncertain'
    assert 'insufficient' in result['summary']


def test_range_union_and_sampling():
    assert p._ranges([{'start_seconds': 0, 'end_seconds': 10}, {'start_seconds': 5, 'end_seconds': 15}], 20) == [(0.0, 15.0)]
    assert p._sample_times([(0, 10), (20, 30)], 4) == [2.5, 7.5, 22.5, 27.5]


def test_transcription_offsets():
    data = {'language': 'en', 'sentences': [{'start_seconds': 2, 'end_seconds': 5, 'text': 'unfinished'}]}
    sentence = p._sentences(data, 120, 180, 'source', 1)[0]
    assert (sentence['start_seconds'], sentence['end_seconds']) == (122, 125)


def test_invalid_model_timestamps_rejected():
    with pytest.raises(ValueError):
        p._sentences({'language': 'en', 'sentences': [{'start_seconds': 0, 'end_seconds': 61, 'text': 'bad'}]}, 0, 60, 'source', 1)


def test_sub_second_end_overshoot_is_clamped_to_segment():
    data = {'language': 'en', 'sentences': [{'start_seconds': 9.42, 'end_seconds': 10.5, 'text': 'La la'}]}
    sentence = p._sentences(data, 0, 10.0, 'clip', 1)[0]
    assert (sentence['start_seconds'], sentence['end_seconds']) == (9.42, 10.0)
    with pytest.raises(ValueError):
        p._sentences({'language': 'en', 'sentences': [{'start_seconds': 10.2, 'end_seconds': 10.5, 'text': 'x'}]}, 0, 10.0, 'clip', 1)


@pytest.fixture
def fake_media(tmp_path, monkeypatch):
    path = tmp_path / 'recording.mp4'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(p, '_probe', lambda path: (130.0, True, False))
    monkeypatch.setattr(p, '_run', lambda args: b'')
    client = Mock()
    monkeypatch.setattr(p, 'client_and_model', lambda stage: (client, 'test-model'))
    monkeypatch.setattr(p, '_ask', lambda *args: {'language': 'en', 'sentences': [{'start_seconds': 0, 'end_seconds': 1, 'text': 'Example speech'}]})
    return {'media_id': 'clip', 'local_path': str(path)}


def test_full_extraction_chunk_offsets(fake_media):
    result = _gemini_features(fake_media, {'extract_frames': False, 'extract_visual_text': False})
    assert result['transcript']['status'] == 'complete'
    assert [s['start_seconds'] for s in result['transcript']['sentences']] == [0, 60, 120]
    assert result['transcript']['processed_ranges'] == [{'start_seconds': 0, 'end_seconds': 130}]


def test_ranged_extraction_is_partial(fake_media):
    result = _gemini_features(fake_media, {'transcript_scope': 'ranges', 'transcript_ranges': [{'start_seconds': 120, 'end_seconds': 130}], 'extract_frames': False, 'extract_visual_text': False})
    assert result['transcript']['status'] == 'partial'
    assert result['transcript']['sentences'][0]['start_seconds'] == 120


def test_provider_failure_is_not_silent_transcript(fake_media, monkeypatch):
    monkeypatch.setattr(p, '_ask', Mock(side_effect=QuantroError('PROCESSING_FAILED', 'extract_media_features', retryable=True)))
    with pytest.raises(QuantroError) as error:
        _gemini_features(fake_media)
    assert error.value.code.value == 'TRANSCRIPTION_FAILED'
    assert error.value.retryable


def test_missing_media_error(tmp_path):
    with pytest.raises(QuantroError) as error:
        _gemini_features({'media_id': 'x', 'local_path': str(tmp_path / 'absent')})
    assert error.value.code.value == 'MEDIA_UNAVAILABLE'


def test_real_ffmpeg_frames_and_audio(tmp_path, monkeypatch):
    """Actual media decoding, fake Gemini response: no network or API credits."""
    import shutil
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg tools not installed')
    video = tmp_path / 'generated.mp4'
    p._run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=160x120:d=2', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(video)])
    client = Mock()
    monkeypatch.setattr(p, 'client_and_model', lambda stage: (client, 'test'))
    def fake_ask(client, model, path, mime, prompt, schema):
        assert path.stat().st_size > 0
        return {'language': None, 'sentences': []} if mime == 'audio/wav' else {'text': ''}
    monkeypatch.setattr(p, '_ask', fake_ask)
    result = _gemini_features({'media_id': 'synthetic', 'local_path': str(video)}, {'max_frames': 2})
    try:
        assert result['transcript']['status'] == 'complete'
        assert len(result['frames']) == 2
        assert all(Path(frame['image_path']).is_file() for frame in result['frames'])
        assert result['visual_text'] == []
    finally:
        if result['frames']:
            shutil.rmtree(Path(result['frames'][0]['image_path']).parent)


def test_not_assessed_stays_not_assessed(clip, context, comparison):
    comparison['context_assessment'] = 'not_assessed'
    comparison['assessment_confidence']['level'] = 'not_assessed'
    comparison['findings'] = []
    result = c._validate_result(comparison, clip, context, None)
    assert result['context_assessment'] == 'not_assessed'
    assert result['assessment_confidence']['level'] == 'not_assessed'


def test_context_rejects_invalid_processed_range(clip, source):
    alignment = verify_and_align(clip, source)
    source['transcript']['processed_ranges'][0]['start_seconds'] = float('nan')
    with pytest.raises(QuantroError) as error:
        select_surrounding_context(alignment, source)
    assert error.value.stage.value == 'select_surrounding_context'


def test_sdk_accepts_structured_output_schemas():
    from google.genai import types
    for schema in (p.TRANSCRIPT_SCHEMA, c.Comparison.model_json_schema()):
        config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=schema)
        assert config.response_mime_type == 'application/json'
