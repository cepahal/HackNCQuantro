"""Regression fixtures: local Whisper small.en on the public NASA JFK sample.

Source https://www.nasa.gov/history/SP-4225/multimedia/before-video.htm
The continuous clip is source 2-24s; the splice joins 2-10s and 16-24s.
These are observed ASR outputs, not hand-corrected ideal transcripts.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.services.specialist.alignment import verify_and_align
from backend.services.specialist.validation import validate_features
from backend.services.processing.preprocessing import _sentences
from backend.services.shared.types import MediaFeatures, TranscriptSentence


@pytest.fixture
def samples():
    return json.loads((Path(__file__).parent / 'fixtures/nasa_word_alignment.json').read_text())


def test_continuous_recording_has_no_source_gap(samples):
    result = verify_and_align(samples['TEST_continuous_trim'], samples['source'])
    assert result['status'] == 'verified'
    assert result['edit_analysis']['source_gaps'] == []
    assert result['match_quality']['clip_coverage_ratio'] > .98
    assert abs(result['matches'][0]['source_start_seconds'] - 2) < .5
    assert abs(result['matches'][0]['source_end_seconds'] - 24) < .5


def test_known_six_second_splice_is_localized(samples):
    result = verify_and_align(samples['TEST_stitched_clip'], samples['source'])
    assert result['status'] == 'verified'
    gaps = result['edit_analysis']['source_gaps']
    assert len(gaps) == 1
    assert abs(gaps[0]['start_seconds'] - 10) < 1
    assert abs(gaps[0]['end_seconds'] - 16) < 1
    assert result['edit_analysis']['counts']['confirmed_deleted_segments'] is None
    # Same sentence can contain words on either side of the splice.
    assert set(result['matches'][0]['evidence'][0]['clip_refs']) & set(result['matches'][1]['evidence'][0]['clip_refs'])
    assert result['matches'][0]['clip_end_seconds'] < result['matches'][1]['clip_start_seconds']


def test_word_times_survive_shared_model_and_legacy_json_is_unchanged(samples):
    result = MediaFeatures.model_validate(samples['source']).model_dump(mode='json')
    assert result['transcript']['sentences'][0]['words'] == samples['source']['transcript']['sentences'][0]['words']
    legacy = {k: v for k, v in samples['source']['transcript']['sentences'][0].items() if k != 'words'}
    assert TranscriptSentence.model_validate(legacy).model_dump(mode='json') == legacy


@pytest.mark.parametrize('mutation', ['text', 'outside', 'overlap', 'missing_word', 'nan'])
def test_invalid_word_metadata_is_rejected(samples, mutation):
    sentence = samples['source']['transcript']['sentences'][0]
    if mutation == 'text':
        sentence['words'][0]['text'] = 'invented'
    elif mutation == 'outside':
        sentence['words'][0]['start_seconds'] = -1
    elif mutation == 'overlap':
        sentence['words'][1]['start_seconds'] = sentence['words'][0]['start_seconds']
    elif mutation == 'missing_word':
        sentence['words'].pop()
    else:
        sentence['words'][0]['end_seconds'] = float('nan')
    with pytest.raises(ValueError):
        validate_features(samples['source'])


def test_word_times_use_full_recording_offsets():
    data = {'language': 'en', 'sentences': [{'text': 'Keep it.', 'start_seconds': 1,
        'end_seconds': 3, 'words': [
            {'text': 'Keep', 'start_seconds': 1, 'end_seconds': 2},
            {'text': 'it.', 'start_seconds': 2, 'end_seconds': 3}]}]}
    result = _sentences(data, 60, 120, 'clip', 1)
    assert result[0]['words'][0]['start_seconds'] == 61
    assert result[0]['words'][1]['end_seconds'] == 63


def test_repeated_timed_passage_is_ambiguous(samples):
    source = deepcopy(samples['source'])
    repeat = deepcopy(source['transcript']['sentences'])
    offset = source['duration_seconds']
    for sentence in repeat:
        sentence['sentence_id'] += '_repeat'
        sentence['start_seconds'] += offset
        sentence['end_seconds'] += offset
        for word in sentence['words']:
            word['start_seconds'] += offset
            word['end_seconds'] += offset
    source['duration_seconds'] *= 2
    source['transcript']['processed_ranges'][0]['end_seconds'] *= 2
    source['transcript']['sentences'] += repeat
    result = verify_and_align(samples['TEST_continuous_trim'], source)
    assert result['status'] == 'no_verified_match'


def test_transcription_difference_does_not_make_a_gap_in_continuous_audio(samples):
    clip = samples['TEST_continuous_trim']
    # Change one middle word consistently in the transcript and word timings.
    sentence = clip['transcript']['sentences'][2]
    old = sentence['words'][3]['text']
    sentence['text'] = sentence['text'].replace(old, 'mistranscribed', 1)
    sentence['words'][3]['text'] = 'mistranscribed'
    result = verify_and_align(clip, samples['source'])
    assert result['status'] == 'verified'
    assert result['match_quality']['clip_coverage_ratio'] < 1
    assert result['edit_analysis']['source_gaps'] == []
