"""Conservative, deterministic transcript alignment (function 6).

Only fully matched submitted sentences are accepted. Word timestamps are not
invented. Repeated source passages are left ambiguous. No audio/visual check
is claimed; accepted transcript matches therefore have moderate confidence.
"""
from difflib import SequenceMatcher
import re

from ..reporting.errors import QuantroError, Stage
from .validation import validate_features, merge_ranges

_STOP = set('a an the and or but if then this that those these it its is are was were be been being to of for in on at as i you he she we they my your our their would could should will can do does did so not no with from by'.split())


def _flatten(features):
    tokens, owners, spans = [], [], []
    sentences = sorted(features['transcript']['sentences'], key=lambda s: (s['start_seconds'], s['end_seconds']))
    for sentence in sentences:
        words = re.findall(r"\w+(?:['’]\w+)?", sentence['text'].casefold())
        start = len(tokens)
        tokens.extend(words)
        owners.extend([sentence] * len(words))
        spans.append((start, len(tokens), sentence))
    return tokens, owners, spans


def _distinctive(words):
    return len(words) >= 8 and len({w for w in words if w not in _STOP}) >= 4


def _occurrences(needle, haystack):
    size = len(needle)
    return sum(haystack[i:i + size] == needle for i in range(len(haystack) - size + 1))


def verify_and_align(clip_features, candidate_features):
    try:
        duration = validate_features(clip_features)
        validate_features(candidate_features)
        return _align(clip_features, candidate_features, duration)
    except (KeyError, TypeError, ValueError):
        raise QuantroError('PROCESSING_FAILED', Stage.VERIFY_AND_ALIGN,
                           'Media features failed alignment validation.') from None


def _align(clip, source, duration):
    a, _, spans = _flatten(clip)
    b, source_owners, _ = _flatten(source)
    unprocessed = any(f['transcript']['status'] == 'unavailable' or not f['transcript']['processed_ranges'] for f in (clip, source))
    limits = ['The earliest original upload was not established.',
              'Visual and audio matching were not performed.',
              'Matching uses whole transcript sentences; timestamps retain transcription precision.']
    for features in (clip, source):
        limits.extend(features.get('warnings', []))
        if features['transcript']['status'] != 'complete':
            limits.append('Transcript coverage is incomplete; unprocessed content was not compared.')
    matches = []
    ambiguous = False
    if not unprocessed:
        for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
            complete = [(left, right, s) for left, right, s in spans
                        if left < right and block.a <= left and right <= block.a + block.size]
            if not complete:
                continue
            left, right = complete[0][0], complete[-1][1]
            words = a[left:right]
            if not _distinctive(words):
                continue
            if _occurrences(words, b) != 1:
                ambiguous = True
                continue
            source_left = block.b + left - block.a
            source_right = block.b + right - block.a
            refs = []
            for sentence in source_owners[source_left:source_right]:
                if sentence['sentence_id'] not in [s['sentence_id'] for s in refs]:
                    refs.append(sentence)
            clip_sentences = [s for _, _, s in complete]
            matches.append({
                'match_id': f'match_{len(matches) + 1:03d}',
                'clip_start_seconds': min(s['start_seconds'] for s in clip_sentences),
                'clip_end_seconds': max(s['end_seconds'] for s in clip_sentences),
                'source_start_seconds': min(s['start_seconds'] for s in refs),
                'source_end_seconds': max(s['end_seconds'] for s in refs),
                'evidence': [{
                    'method': 'transcript_alignment',
                    'clip_refs': [s['sentence_id'] for s in clip_sentences],
                    'source_refs': [s['sentence_id'] for s in refs],
                    'description': 'A distinctive word sequence matches exactly after case and punctuation normalization. Source bounds use enclosing sentence timestamps.',
                }],
            })
    if ambiguous:
        limits.append('A repeated passage has multiple source locations; its alignment was not accepted.')
    if not matches:
        limits.append('No verified match does not establish that the submitted clip is authentic or unedited.')
    intervals = merge_ranges([(m['clip_start_seconds'], m['clip_end_seconds']) for m in matches])
    seconds = sum(end - start for start, end in intervals)
    gaps, unmatched = [], []
    assessed = bool(matches) and clip['transcript']['status'] == 'complete' and source['transcript']['status'] == 'complete'
    if assessed:
        cursor = 0.0
        for start, end in intervals:
            if start > cursor:
                unmatched.append({'start_seconds': cursor, 'end_seconds': start})
            cursor = max(cursor, end)
        if cursor < duration:
            unmatched.append({'start_seconds': cursor, 'end_seconds': duration})
        for previous, following in zip(matches, matches[1:]):
            start, end = previous['source_end_seconds'], following['source_start_seconds']
            if end - start >= 1.0:
                gaps.append({'start_seconds': start, 'end_seconds': end,
                             'before_match_id': previous['match_id'], 'after_match_id': following['match_id']})
            elif following['source_start_seconds'] < previous['source_start_seconds']:
                limits.append('Matched source passages occur in a different order; deletion counts are not established.')
    if len(matches) == 1:
        limits.append('Only single-passage alignment was evaluated.')
    level = 'not_assessed' if unprocessed else ('moderate' if matches else 'low')
    return {
        'status': 'not_assessed' if unprocessed else ('verified' if matches else 'no_verified_match'),
        'clip_media_id': clip['media_id'], 'source_media_id': source['media_id'],
        'matches': matches,
        'match_quality': {
            'confidence': {
                'level': level, 'method': 'evidence_rules_v1',
                'basis': ['Distinctive submitted speech aligns to a unique source passage.'] if matches else [],
                'limitations': list(dict.fromkeys(limits)),
            },
            'matched_clip_seconds': seconds if matches else None,
            'clip_coverage_ratio': seconds / duration if matches else None,
            'metrics': [], 'methods_used': [] if unprocessed else ['transcript_alignment'],
        },
        'edit_analysis': {
            'status': 'assessed' if assessed else 'not_assessed',
            'scope': 'between_matched_passages', 'minimum_gap_seconds': 1.0,
            'source_gaps': gaps, 'unmatched_clip_ranges': unmatched,
            'counts': {
                'matched_passages': len(matches), 'source_gaps': len(gaps) if assessed else None,
                'unmatched_clip_segments': len(unmatched) if assessed else None,
                'confirmed_added_segments': None, 'confirmed_deleted_segments': None,
            },
        },
        'limitations': list(dict.fromkeys(limits)),
    }
