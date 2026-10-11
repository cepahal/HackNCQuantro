"""Convert prototype media/transcripts into the existing strict public contracts."""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
from datetime import UTC, datetime
from typing import Any

from pydantic import PrivateAttr

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
)
from .download import (
    MediaDownloadError,
    detect_platform,
    download_short_media,
    fetch_youtube_oembed,
)
from .transcribe import TranscriptionError, extract_youtube_id, get_video_transcript


class _ResolvedFeatures(MediaFeatures):
    """Carry complete discovery text privately without changing report JSON."""

    _full_transcript_text: str = PrivateAttr(default="")

    @property
    def full_transcript_text(self) -> str:
        return self._full_transcript_text


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


async def _transcript_for_media(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Separate failed media access from speech extraction without provider text."""
    try:
        return await get_video_transcript(*args, **kwargs)
    except QuantroError:
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
        raise error from exc


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
    if transcript_result is not None:
        transcript = dict(transcript_result)
        if platform == "youtube":
            warnings.append(
                "Source playback uses YouTube; its video file was not downloaded. Analysis uses its timed transcript."
            )
    elif platform == "youtube":
        # Follow the prototype: acquire the submitted clip, then prefer captions.
        # A blocked recording must not discard independently available captions.
        if os.getenv("RESOLUTION_DOWNLOAD_VIDEO", "true").lower() == "true":
            try:
                download = await download_short_media(url)
                info = {**download, **info}
            except MediaDownloadError:
                warnings.append(
                    "YouTube blocked the video download. Available captions can still be matched; audio and visual forensics were not assessed."
                )
        else:
            warnings.append(
                "YouTube playback uses the original recording; the video file was not downloaded."
            )
        transcript = await _transcript_for_media(
            url, download["file_path"] if download else None, known_info=info or None
        )
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
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        start, end = _number(segment.get("start")), _number(segment.get("end"))
        text = _text(segment.get("text"))
        if start is not None and end is not None and end > start and text:
            sentences.append(
                TranscriptSentence(
                    sentence_id=f"s{len(sentences) + 1}",
                    start_seconds=start,
                    end_seconds=end,
                    text=text,
                    speaker_id=None,
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
            ["media.local_path", "media.width", "media.height", "media.fps"]
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
    return post, features
