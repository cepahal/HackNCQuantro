"""Optional local speech recognition. No API key or silent provider fallback."""
from functools import lru_cache
import os

from .gemini import load_settings


def provider():
    load_settings()
    value = (os.getenv('TRANSCRIPTION_PROVIDER') or 'gemini').strip().lower()
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
    sentences = []
    for segment in segments:
        if not segment.text.strip():
            continue
        # Retain the complete token sequence or no word timings. Zero-length
        # estimates cannot support interval matching; never invent durations.
        raw_words = getattr(segment, 'words', None) or []
        words = [{'text': w.word.strip(), 'start_seconds': float(w.start),
                  'end_seconds': float(w.end)} for w in raw_words]
        if any(not w['text'] or w['end_seconds'] <= w['start_seconds'] for w in words):
            words = []
        sentences.append({'text': segment.text.strip(),
            'start_seconds': float(segment.start), 'end_seconds': float(segment.end), 'words': words})
    return {'language': info.language, 'sentences': sentences}
