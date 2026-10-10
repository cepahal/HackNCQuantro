"""Optional local speech recognition. No API key or silent provider fallback."""
import math
import os
from functools import lru_cache

from .gemini import load_settings


def provider():
    load_settings()
    value = (os.getenv('TRANSCRIPTION_PROVIDER') or 'whisper').strip().lower()
    if value not in ('gemini', 'whisper'):
        raise ValueError('TRANSCRIPTION_PROVIDER must be gemini or whisper')
    return value


@lru_cache(maxsize=2)
def _model(name):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device='cpu', compute_type='int8')


def transcribe(path):
    # Load/download lazily, once per process. Decode speech as spoken rather
    # than supplying source text, which could bias a clip toward that source.
    model = _model(os.getenv('WHISPER_MODEL') or 'small')
    segments, info = model.transcribe(str(path), beam_size=5,
        word_timestamps=True, condition_on_previous_text=False)
    duration = getattr(info, 'duration', None)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        duration = None
    sentences = []
    for segment in segments:
        if not segment.text.strip():
            continue
        start, end = float(segment.start), float(segment.end)
        # ASR may include padded decoding time beyond the actual audio. Keep
        # observed bounds without manufacturing additional recording duration.
        if duration is not None:
            end = min(end, duration)
            if start >= end:
                continue
        # Retain the complete token sequence or no word timings. Zero-length
        # estimates cannot support interval matching; never invent durations.
        raw_words = getattr(segment, 'words', None) or []
        words = [{'text': w.word.strip(), 'start_seconds': float(w.start),
                  'end_seconds': float(w.end)} for w in raw_words]
        if duration is not None:
            for word in words:
                word['end_seconds'] = min(word['end_seconds'], end)
        if any(not w['text'] or not start <= w['start_seconds'] < w['end_seconds'] <= end for w in words):
            words = []
        sentences.append({'text': segment.text.strip(),
            'start_seconds': start, 'end_seconds': end, 'words': words})
    return {'language': info.language, 'sentences': sentences}
