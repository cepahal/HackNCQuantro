"""Optional local speech recognition. No API key or silent provider fallback."""
from functools import lru_cache
import os
import hashlib
import logging
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)
_TRANSCRIPTS = OrderedDict()
_CACHE_LOCK = Lock()
_CACHE_LIMIT = 128

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
    """Cache identical audio by content and model, never by temporary pathname."""
    name = os.getenv('WHISPER_MODEL') or 'small'
    digest = hashlib.sha256()
    with Path(path).open('rb') as audio:
        for block in iter(lambda: audio.read(1024 * 1024), b''):
            digest.update(block)
    key = (name, digest.digest())
    started = time.monotonic()
    with _CACHE_LOCK:
        if key in _TRANSCRIPTS:
            _TRANSCRIPTS.move_to_end(key)
            logger.info("whisper_segment cache=hit seconds=%.3f", time.monotonic() - started)
            return deepcopy(_TRANSCRIPTS[key])
    result = _transcribe_uncached(path, name)
    with _CACHE_LOCK:
        _TRANSCRIPTS[key] = deepcopy(result)
        _TRANSCRIPTS.move_to_end(key)
        while len(_TRANSCRIPTS) > _CACHE_LIMIT:
            _TRANSCRIPTS.popitem(last=False)
    logger.info("whisper_segment cache=miss seconds=%.3f", time.monotonic() - started)
    return result


def _transcribe_uncached(path, name):
    # Load/download lazily, once per process. Decode speech as spoken rather
    # than supplying source text, which could bias a clip toward that source.
    model = _model(name)
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
