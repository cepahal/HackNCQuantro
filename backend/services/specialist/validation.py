"""Validation shared by the specialist stages; no network calls."""
import math


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Expected finite numeric seconds.')
    return float(value)


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def validate_features(features):
    if not isinstance(features['media_id'], str) or not features['media_id']:
        raise ValueError('media_id is required.')
    duration = number(features['duration_seconds'])
    if duration < 0:
        raise ValueError('Duration cannot be negative.')
    transcript = features['transcript']
    if duration == 0 and transcript['status'] != 'unavailable':
        raise ValueError('Zero-duration media cannot have a processed transcript.')
    if transcript['status'] not in ('complete', 'partial', 'unavailable'):
        raise ValueError('Invalid transcript status.')
    processed = []
    for item in transcript['processed_ranges']:
        start, end = number(item['start_seconds']), number(item['end_seconds'])
        if not 0 <= start < end <= duration:
            raise ValueError('Processed range is outside recording.')
        processed.append((start, end))
    processed = merge_ranges(processed)
    if transcript['status'] == 'complete' and processed != [(0.0, duration)]:
        raise ValueError('Complete transcript must cover the recording.')
    if transcript['status'] == 'unavailable' and (processed or transcript['sentences']):
        raise ValueError('Unavailable transcript cannot contain processed data.')
    seen = set()
    for sentence in transcript['sentences']:
        start, end = number(sentence['start_seconds']), number(sentence['end_seconds'])
        if not 0 <= start < end <= duration:
            raise ValueError('Sentence is outside recording.')
        if not any(left <= start and end <= right for left, right in processed):
            raise ValueError('Sentence is outside processed data.')
        identity = sentence['sentence_id']
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('Sentence IDs must be unique and nonempty.')
        seen.add(identity)
        if not isinstance(sentence['text'], str) or not sentence['text'].strip():
            raise ValueError('Sentence text must be nonempty.')
    return duration
