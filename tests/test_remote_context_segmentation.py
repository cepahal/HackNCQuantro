"""Observed context may share a sentence with matched speech; no word times invented."""

from copy import deepcopy

import pytest

from backend.services.reporting.remote_source_analysis import _has_surrounding_speech
from backend.services.shared.types import Alignment, Transcript
from backend.services.specialist.alignment import _align

SPEECH = 'If the employment numbers were accurate we would reconsider the public program.'


def transcript(text, identifier, start=0.0, end=20.0):
    return Transcript.model_validate({
        'status': 'partial', 'processed_ranges': [{'start_seconds': start, 'end_seconds': end}],
        'sentences': [{'sentence_id': identifier, 'start_seconds': start, 'end_seconds': end, 'text': text}],
    })


def alignment(clip, source):
    return Alignment.model_validate(_align(
        {'media_id': 'clip', 'transcript': clip.model_dump(mode='json')},
        {'media_id': 'source', 'transcript': source.model_dump(mode='json')}, None,
    ))


@pytest.mark.parametrize('text', [
    'Earlier independent audit findings contradicted these assumptions. ' + SPEECH,
    SPEECH + ' The independent audit subsequently rejected those estimates.',
    'Those figures are not accurate. ' + SPEECH,
])
def test_substantive_prefix_or_suffix_inside_matched_sentence_is_observed_context(text):
    clip = transcript(SPEECH, 'clip1')
    source = transcript(text, 'source1', 754.0, 774.0)
    before = deepcopy((clip.model_dump(mode='json'), source.model_dump(mode='json')))
    matched = alignment(clip, source)
    assert matched.status == 'verified'
    assert matched.matches[0].evidence[0].source_refs == ['source1']
    assert _has_surrounding_speech(clip, source, matched)
    assert (clip.model_dump(mode='json'), source.model_dump(mode='json')) == before
    assert source.sentences[0].words == []
    assert matched.matches[0].source_start_seconds == 754.0
    assert matched.matches[0].source_end_seconds == 774.0


@pytest.mark.parametrize('text', [
    SPEECH, SPEECH + ' ' + SPEECH,
    'Thank you very much. ' + SPEECH,
    SPEECH + ' Thank you everyone for watching this video today.',
    SPEECH + ' Please subscribe to our channel for more interesting daily videos.',
    'Thanks. ' + SPEECH,
    SPEECH + ' Audits changed.',
])
def test_copy_boilerplate_or_insufficient_extra_speech_stays_contextless(text):
    clip = transcript(SPEECH, 'clip1')
    source = transcript(text, 'source1')
    assert not _has_surrounding_speech(clip, source, alignment(clip, source))


def test_internal_transcription_difference_does_not_become_surrounding_context():
    clip = transcript(SPEECH, 'clip1')
    source = transcript(SPEECH.replace('accurate', 'accurate independent investigators found major errors'), 'source1')
    matched = alignment(clip, source)
    assert not _has_surrounding_speech(clip, source, matched)


def test_short_common_word_overlap_cannot_justify_context_with_shared_sentence_id():
    clip = transcript('The public program is under review.', 'clip1')
    source = transcript('Earlier independent audit findings contradicted these assumptions. The public program is under review.', 'source1')
    matched = alignment(clip, source)
    assert matched.status == 'no_verified_match'
    assert not _has_surrounding_speech(clip, source, matched)


def test_distinct_separate_context_outside_padding_remains_excluded():
    clip = transcript(SPEECH, 'clip1', 0.0, 5.0)
    source = transcript(SPEECH, 'source1', 0.0, 5.0)
    source.sentences.append(source.sentences[0].model_copy(update={
        'sentence_id': 'distant', 'start_seconds': 50.0, 'end_seconds': 58.0,
        'text': 'The independent audit subsequently rejected those estimates.',
    }))
    assert not _has_surrounding_speech(clip, source, alignment(clip, source))
