"""Convert prototype media/transcripts into the existing strict public contracts."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import PrivateAttr, ValidationError

from ..external_apis.retrieve_post import _source_url
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..shared.types import (
    Author,
    Engagement,
    MediaAsset,
    MediaFeatures,
    Post,
    Retrieval,
    TimeRange,
    Transcript,
    TranscriptSentence,
    TranscriptStatus,
    TranscriptWord,
)
from .diagnostics import record
from .download import (
    MediaDownloadError,
    detect_platform,
    download_short_media,
    fetch_youtube_oembed,
)
from .transcribe import (
    TranscriptionError,
    _timed_speech_words,
    extract_youtube_id,
    get_video_transcript,
)

_Result = TypeVar("_Result")


def _temporary_transcription_failure(error: Exception) -> bool:
    if isinstance(error, TranscriptionError):
        return error.code in {"network_error", "rate_limited"}
    if isinstance(error, QuantroError):
        return (
            error.retryable
            and error.stage == Stage.EXTRACT_MEDIA_FEATURES
            and error.code in {ErrorCode.TRANSCRIPTION_FAILED, ErrorCode.PROCESSING_FAILED}
        )
    return isinstance(error, (TimeoutError, ConnectionError, subprocess.TimeoutExpired))


async def _retry_transcription(
    operation: Callable[[], Awaitable[_Result]], *, stage: str
) -> _Result:
    """Retry temporary speech-provider failures twice, without restarting the job."""
    for attempt in range(3):
        try:
            return await operation()
        except Exception as error:
            if attempt == 2 or not _temporary_transcription_failure(error):
                raise
            record(stage, "retrying", {
                "reason": "temporary_transcription_failure", "retryable": True,
            })
            await asyncio.sleep(3)
    raise AssertionError("Unreachable transcription retry state")


class _ResolvedFeatures(MediaFeatures):
    """Carry complete discovery text privately without changing report JSON."""

    _full_transcript_text: str = PrivateAttr(default="")
    _discovery_hints: list[str] = PrivateAttr(default_factory=list)
    _word_confidences: dict[str, list[dict[str, float]]] = PrivateAttr(default_factory=dict)

    @property
    def discovery_hints(self) -> list[str]:
        """Unverified same-call visual discovery clues, excluded from public reports."""
        return list(self._discovery_hints)

    @property
    def full_transcript_text(self) -> str:
        return self._full_transcript_text

    @property
    def word_confidences(self) -> dict[str, list[dict[str, float]]]:
        """Provider metadata only; the shared word/report contract has no confidence field."""
        return self._word_confidences


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and not number):
        return None
    return number


def _count(value: Any) -> int | None:
    number = _number(value, positive=True)
    return int(number) if number is not None and number.is_integer() else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _timed_words(
    segment: dict[str, Any], text: str, start: float, end: float
) -> tuple[list[TranscriptWord], list[dict[str, float]]]:
    """Keep a complete observed word sequence or use enclosing segment timing.

    Accept shared-contract and provider spellings; never interpolate, reorder,
    clamp, or retain a misleading partial word sequence.
    """
    raw_words = segment.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        return [], []
    words: list[TranscriptWord] = []
    confidences: list[dict[str, float]] = []
    previous = start
    for item in raw_words:
        if not isinstance(item, dict):
            return [], []
        word_text = _text(item.get("text", item.get("word")))
        left = _number(item.get("start_seconds", item.get("start")))
        right = _number(item.get("end_seconds", item.get("end")))
        if (
            word_text is None or left is None or right is None
            or not start <= left < right <= end or left < previous
        ):
            return [], []
        try:
            word = TranscriptWord.model_validate(
                {"text": word_text, "start_seconds": left, "end_seconds": right}
            )
        except ValidationError:
            return [], []
        words.append(word)
        previous = right
        confidence = {}
        for key in ("confidence", "probability"):
            value = _number(item.get(key))
            if value is not None and value <= 1:
                confidence[key] = value
        confidences.append(confidence)
    # Same token policy as specialist validation; original word text stays intact.
    pattern = r"\w+(?:['’]\w+)?"
    supplied_tokens = re.findall(pattern, " ".join(word.text for word in words).casefold())
    if supplied_tokens != re.findall(pattern, text.casefold()):
        return [], []
    return words, confidences


async def _transcript_for_media(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Separate failed media access from speech extraction without provider text."""
    started = time.monotonic()
    record("transcribe_media", "started", {})
    try:
        result = await _retry_transcription(
            lambda: get_video_transcript(*args, **kwargs), stage="transcribe_media",
        )
        record("transcribe_media", "completed", {
            "transcript_source": result.get("source"),
        }, started=started)
        return result
    except QuantroError as error:
        record("transcribe_media", "failed", {
            "code": error.code.value, "retryable": error.retryable,
        }, started=started)
        raise
    except Exception as exc:
        retrieval_failed = isinstance(exc, TranscriptionError) and exc.code in {
            "auth_required",
            "private",
            "geo_restricted",
            "unavailable",
            "format_unavailable",
            "download_failed",
        }
        retryable = False
        cause: BaseException | None = exc
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            retryable = retryable or (
                isinstance(
                    cause, (TimeoutError, ConnectionError, subprocess.TimeoutExpired)
                )
                or getattr(cause, "retryable", False) is True
                or getattr(cause, "code", None) in {"network_error", "rate_limited"}
            )
            cause = cause.__cause__
        error = QuantroError(
            ErrorCode.MEDIA_UNAVAILABLE
            if retrieval_failed
            else ErrorCode.TRANSCRIPTION_FAILED,
            Stage.RETRIEVE_POST if retrieval_failed else Stage.EXTRACT_MEDIA_FEATURES,
            retryable=retryable,
        )
        error.exception_type = type(exc).__name__
        if isinstance(exc, TranscriptionError):
            # Private typed diagnostics survive normalization, never provider text.
            error.retrieval_reason = exc.code
            error.platform_challenge = (
                exc.code == "auth_required"
                or getattr(exc, "platform_challenge", False) is True
            )
        record("transcribe_media", "failed", {
            "code": error.code.value, "retryable": error.retryable,
        }, started=started)
        raise error from exc


async def _native_youtube_transcript(
    url: str,
    *,
    reason: str,
    discovery_hints: list[str],
    warnings: list[str],
    minimum_timed_words: int = 0,
) -> dict[str, Any]:
    """Read one bounded public window without promoting metadata into speech."""
    from ..reporting.remote_youtube import (
        WINDOW_SECONDS,
        build_remote_youtube_report,
        youtube_window_start,
    )

    try:
        window_start = youtube_window_start(url)
    except ValueError:
        raise QuantroError(ErrorCode.INVALID_URL, Stage.RETRIEVE_POST) from None
    window_options = {"window_start_seconds": window_start} if window_start else {}
    fallback_started = time.monotonic()
    record("native_youtube_fallback", "started", {
        "reason": reason, "start_seconds": window_start,
        "end_seconds": window_start + WINDOW_SECONDS,
    })

    async def read_window():
        attempt_hints: list[str] = []
        result = await asyncio.to_thread(
            build_remote_youtube_report, "retrieval_fallback", url,
            discovery_hints=attempt_hints, **window_options,
        )
        discovery_hints.extend(attempt_hints)
        return result

    try:
        remote = await _retry_transcription(
            read_window, stage="native_youtube_fallback",
        )
    except QuantroError as error:
        safe_message = error.message if re.fullmatch(
            r"Gemini (?:request failed(?: \(HTTP [1-5][0-9]{2}\))?|request timed out|connection failed)\.",
            error.message,
        ) else QuantroError(error.code, error.stage).message
        record("native_youtube_fallback", "failed", {
            "code": error.code.value, "stage": error.stage.value,
            "retryable": error.retryable, "message": safe_message,
        }, started=fallback_started)
        raise
    except Exception as error:  # noqa: BLE001 - Record type only; caller decides whether recovery is optional.
        record("native_youtube_fallback", "failed", {
            "exception_type": type(error).__name__,
        }, started=fallback_started)
        raise
    speech = remote.submitted.transcript.sentences
    transcript = {
        "transcript": " ".join(sentence.text for sentence in speech),
        "segments": [{"text": sentence.text, "start": sentence.start_seconds,
                      "end": sentence.end_seconds} for sentence in speech],
        "source": "gemini_native_youtube_transcript", "coverage": "partial",
        "duration_known": False, "title": remote.submitted.post.title,
    }
    if _timed_speech_words(transcript) <= minimum_timed_words:
        record("native_youtube_fallback", "unavailable", {
            "reason": "no_additional_timed_speech" if minimum_timed_words
            else "no_usable_transcript",
        }, started=fallback_started)
        raise QuantroError(ErrorCode.TRANSCRIPTION_FAILED, Stage.EXTRACT_MEDIA_FEATURES)
    record("native_youtube_fallback", "completed", {
        "coverage": "partial", "start_seconds": window_start,
        "end_seconds": window_start + WINDOW_SECONDS,
    }, started=fallback_started)
    observed_window = (
        f"the window from {window_start} to {window_start + int(WINDOW_SECONDS)} seconds"
        if window_start else "the first 60 seconds"
    )
    warnings.extend([
        f"Local YouTube speech retrieval was limited; Gemini supplied a partial transcript from {observed_window}.",
        "Gemini transcript words and timestamps are estimates; audio and visual authenticity were not assessed.",
    ])
    if discovery_hints:
        warnings.append("Unverified visual search clues supplement discovery only; they do not establish identity, dates, speech or source context.")
    return transcript


async def resolve_media(
    url: str,
    *,
    candidate_id: str | None = None,
    transcript_result: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Post, MediaFeatures]:
    """Caption-first YouTube resolution; candidate transcripts avoid extra downloads."""
    platform = detect_platform(url)
    url = url.strip()
    info = dict(metadata or {})
    download: dict[str, Any] | None = None
    warnings: list[str] = []
    discovery_hints: list[str] = []
    download_challenged = False
    download_recoverable = False
    download_terminal = False
    if transcript_result is not None:
        transcript = dict(transcript_result)
        if platform == "youtube":
            warnings.append(
                "Source playback uses YouTube; its video file was not downloaded. Analysis uses the supplied transcript; usable timestamps are checked separately."
            )
    elif platform == "youtube":
        # Follow the prototype: acquire the submitted clip, then prefer captions.
        # A blocked recording must not discard independently available captions.
        download_enabled = os.getenv("RESOLUTION_DOWNLOAD_VIDEO", "true").lower() == "true"
        if download_enabled:
            download_started = time.monotonic()
            try:
                download = await download_short_media(url)
                info = {**download, **info}
            except MediaDownloadError as exc:
                download_challenged = exc.code == "auth_required"
                download_recoverable = exc.code in {
                    "auth_required", "download_failed", "format_unavailable",
                }
                download_terminal = exc.code in {
                    "private", "unavailable", "geo_restricted", "file_too_large",
                }
                record("retrieve_video", "unavailable", {
                    "code": exc.code if exc.code in {
                        "auth_required", "private", "geo_restricted", "unavailable",
                        "format_unavailable", "download_failed", "file_too_large",
                    } else "download_failed",
                    "reason": "platform_challenge" if download_challenged else "video_download_unavailable",
                }, started=download_started)
                warnings.append(
                    ("YouTube blocked the video download." if download_challenged
                     else "The YouTube video file could not be downloaded.")
                    + " Available captions can still be matched; audio and visual forensics were not assessed."
                )
        else:
            warnings.append(
                "YouTube playback uses the original recording; the video file was not downloaded."
            )
        try:
            transcript = await _transcript_for_media(
                url, download["file_path"] if download else None, known_info=info or None
            )
        except QuantroError as error:
            reason = getattr(error, "retrieval_reason", None)
            # Independent native-video reading can recover acquisition failures,
            # including format/network failures without an explicit bot message.
            # It must not retry existing-file ASR or terminal/provider failures.
            recoverable_after_download = download_recoverable and reason in {
                "auth_required", "download_failed", "format_unavailable", "missing_api_key",
            }
            terminal_failure = download_terminal or reason in {
                "private", "unavailable", "geo_restricted", "no_audio",
                "auth_failed", "rate_limited", "file_too_large",
            }
            challenged = download_challenged or getattr(error, "platform_challenge", False)
            fallback_allowed = download is None and not terminal_failure and (
                error.code == ErrorCode.MEDIA_UNAVAILABLE and (
                    challenged or reason in {"auth_required", "download_failed", "format_unavailable"}
                ) or recoverable_after_download
            )
            fallback_reason = "platform_challenge" if challenged else "video_download_unavailable"
            record("retrieve_transcript", "unavailable", {
                "code": error.code.value,
                "reason": fallback_reason if fallback_allowed else "transcript_unavailable",
                "retryable": error.retryable,
            })
            if not fallback_allowed:
                raise
            try:
                transcript = await _native_youtube_transcript(
                    url, reason=fallback_reason,
                    discovery_hints=discovery_hints, warnings=warnings,
                )
            except QuantroError as fallback_error:
                if fallback_error.code == ErrorCode.INVALID_URL:
                    raise
                raise error from error.__cause__
        if (
            transcript.get("discovery_inadequate") is True
            and download is None
            and not download_terminal
            and transcript.get("discovery_recovery_error") not in {
                "private", "unavailable", "geo_restricted", "no_audio",
                "auth_failed", "rate_limited", "file_too_large",
            }
            and (download_recoverable or not download_enabled)
        ):
            # Sparse captions are useful evidence, but may omit the quotation.
            # Preserve them unless one independent window adds timed speech.
            recovery_warnings: list[str] = []
            recovery_hints: list[str] = []
            try:
                recovered = await _native_youtube_transcript(
                    url, reason="sparse_caption_evidence",
                    discovery_hints=recovery_hints, warnings=recovery_warnings,
                    minimum_timed_words=_timed_speech_words(transcript),
                )
            except QuantroError as recovery_error:
                if recovery_error.code == ErrorCode.INVALID_URL:
                    raise
                warnings.append("Independent video reading did not add usable timed speech; the available captions were retained.")
            except Exception:  # noqa: BLE001 - Optional recovery cannot discard observed captions.
                warnings.append("Independent video reading did not add usable timed speech; the available captions were retained.")
            else:
                warnings.extend(warning for warning in transcript.get("warnings", [])
                                if isinstance(warning, str))
                warnings.extend(recovery_warnings)
                discovery_hints.extend(recovery_hints)
                transcript = recovered
        if not info.get("title") and not transcript.get("title"):
            try:
                info = {**(await fetch_youtube_oembed(url)), **info}
            except MediaDownloadError:
                warnings.append(
                    "YouTube title and profile metadata could not be retrieved."
                )
    else:
        download = await download_short_media(url)
        info = {**download, **info}
        transcript = await _transcript_for_media(
            url, download["file_path"], known_info=info
        )

    warnings.extend(
        warning
        for warning in transcript.get("warnings", [])
        if isinstance(warning, str)
    )
    identity = hashlib.sha256(
        f"{platform}:{url}:{candidate_id or ''}".encode()
    ).hexdigest()[:24]
    media_id = f"media_{identity}"
    sentences: list[TranscriptSentence] = []
    word_confidences: dict[str, list[dict[str, float]]] = {}
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        start, end = _number(segment.get("start")), _number(segment.get("end"))
        text = _text(segment.get("text"))
        if start is not None and end is not None and end > start and text:
            words, confidences = _timed_words(segment, text, start, end)
            sentence_id = f"s{len(sentences) + 1}"
            if segment.get("words") and not words:
                warnings.append(
                    "Invalid or incomplete provider word timestamps were ignored; enclosing segment timing is retained."
                )
            if words:
                word_confidences[sentence_id] = confidences
            sentences.append(
                TranscriptSentence(
                    sentence_id=sentence_id,
                    start_seconds=start,
                    end_seconds=end,
                    text=text,
                    speaker_id=None,
                    words=words,
                )
            )
    sentences.sort(key=lambda item: (item.start_seconds, item.end_seconds))
    transcript_end = max((sentence.end_seconds for sentence in sentences), default=0.0)
    recording_duration = (
        None
        if transcript.get("duration_hint_rejected") is True
        else _number(info.get("duration"), positive=True)
    )
    if recording_duration is None and transcript.get("duration_known") is not False:
        recording_duration = _number(transcript.get("duration"), positive=True)
    duration = max(
        recording_duration or 0.0,
        _number(transcript.get("duration")) or 0.0,
        transcript_end,
    )
    if sentences:
        known_duration = _number(info.get("duration"))
        coverage_known = (
            transcript.get("coverage") != "partial"
            and known_duration is not None
            and known_duration > 0
            and sentences[0].start_seconds <= 3
            and transcript_end >= known_duration - 3
        )
        status = (
            TranscriptStatus.COMPLETE if coverage_known else TranscriptStatus.PARTIAL
        )
        if not coverage_known:
            warnings.append(
                "The provider supplied a timed transcript, but complete recording coverage could not be independently confirmed."
            )
        ranges = [
            TimeRange(
                start_seconds=min(sentence.start_seconds for sentence in sentences),
                end_seconds=transcript_end,
            )
        ]
    else:
        status = TranscriptStatus.UNAVAILABLE
        ranges = []
        warnings.append(
            "No provider supplied usable transcript timestamps. Transcript alignment is unavailable."
        )
    if (transcript.get("transcript") or "").strip() and not sentences:
        status = TranscriptStatus.PARTIAL
    video_id = extract_youtube_id(url) if platform == "youtube" else None
    playback_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    if download:
        local_path = download["file_path"]
    else:
        # Internal contract requires a string; the public report strips this field.
        # Empty means no local artifact and must never be served as a filesystem path.
        local_path = ""
    unavailable = [
        "published_at",
        "author.verified",
        "author.followers_count",
        "author.following_count",
        "author.posts_count",
        "engagement.views_count",
        "engagement.likes_count",
        "engagement.comments_count",
        "engagement.shares_count",
    ]
    if not download:
        unavailable.extend(
            ["media.width", "media.height", "media.fps"]
        )
    warnings.append(
        "Frame extraction and visual forensics were not run; the assessment is based on transcript evidence."
    )
    warnings.append(
        "Transcript timestamps come from the caption or speech provider and were not independently verified against the recording."
    )
    post = Post(
        post_id=f"post_{identity}",
        candidate_id=candidate_id,
        post_url=url,
        platform=platform,
        title=_text(info.get("title")) or _text(transcript.get("title")),
        caption=_text(info.get("caption")),
        author=Author(
            id=_text(info.get("author_id")),
            display_name=_text(info.get("author_name")),
            profile_url=_source_url(info.get("author_url")),
        ),
        engagement=Engagement(),
        published_at=None,
        media=MediaAsset(
            media_id=media_id,
            local_path=local_path,
            mime_type="video/mp4",
            playback_url=playback_url,
            duration_seconds=recording_duration,
            width=_count(info.get("width")) if download else None,
            height=_count(info.get("height")) if download else None,
            fps=_number(info.get("fps"), positive=True) if download else None,
            has_audio=True if transcript.get("source") == "groq_whisper" else None,
        ),
        retrieval=Retrieval(
            retrieved_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            provider=f"yt-dlp+{transcript.get('source') or 'transcript'}",
            metadata_source_urls=[url],
            unavailable_fields=unavailable,
            warnings=list(warnings),
        ),
    )
    features = _ResolvedFeatures(
        media_id=media_id,
        duration_seconds=float(duration),
        language=None,
        transcript=Transcript(
            status=status, processed_ranges=ranges, sentences=sentences
        ),
        frames=[],
        visual_text=[],
        warnings=list(warnings),
    )
    features._full_transcript_text = _text(transcript.get("transcript")) or ""
    features._word_confidences = word_confidences
    features._discovery_hints = discovery_hints
    return post, features
