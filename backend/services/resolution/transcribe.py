"""Caption-first timed transcripts, persistent cache, and chunked Groq Whisper."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from groq import (
    APIConnectionError,
    APIStatusError,
    AsyncGroq,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from .captions_provider import CaptionProviderError, fetch_caption_transcript
from .download import (
    MediaDownloadError,
    _build_ydl_opts,
    _find_tool,
    _guarded_call,
    cache_root,
    detect_platform,
    download_short_media,
)

logger = logging.getLogger(__name__)
WHISPER_MODEL = "whisper-large-v3-turbo"
_CAPTION_CACHE_DIR = cache_root() / "captions"
_CAPTION_LANGS = ["en", "en-US", "en-GB"]
_CAPTION_NOISE = re.compile(
    r"\[[^\]]*\]|\((?:music|applause|laughter|laughs)\)|[♪♫]", re.IGNORECASE
)
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BLOCK_BACKOFF_S = 30 * 60
_transcript_api_blocked_until = 0.0
_ytdlp_captions_blocked_until = 0.0
_CHUNK_SECONDS = 20 * 60
_AUDIO_EXTS = {
    ".m4a",
    ".mp3",
    ".mpga",
    ".mpeg",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
}
_MAX_UPLOAD_BYTES = int(float(os.getenv("GROQ_MAX_UPLOAD_MB", "25")) * 1024 * 1024)


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, url: str, platform: str | None = None):
        super().__init__(message)
        self.code, self.message, self.url, self.platform = code, message, url, platform
        self.platform_challenge = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "url": self.url,
            "platform": self.platform,
        }


class CachedNoCaptions(CouldNotRetrieveTranscript):
    CAUSE_MESSAGE = "A previous lookup found no usable captions."


class NoCaptionsViaYtDlp(CouldNotRetrieveTranscript):
    CAUSE_MESSAGE = "No usable caption track was found."


_PERMANENT_CAPTION_ERRORS = (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    InvalidVideoId,
    NoCaptionsViaYtDlp,
)


def extract_youtube_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    candidate: str | None = None
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            query_ids = parse_qs(parsed.query).get("v", [])
            candidate = query_ids[0] if query_ids else None
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    return candidate if candidate and _YT_ID.fullmatch(candidate) else None


def _clean_text(text: str) -> str:
    return re.sub(
        r"\s+", " ", _CAPTION_NOISE.sub(" ", html.unescape(text)).replace(">>", " ")
    ).strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _segments(values: Any, *, offset: float = 0.0) -> list[dict[str, Any]]:
    result = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        text = _clean_text(str(item.get("text") or ""))
        start, end = _number(item.get("start")), _number(item.get("end"))
        if text and start is not None and end is not None and end > start:
            result.append(
                {
                    "text": text,
                    "start": float(start + offset),
                    "end": float(end + offset),
                }
            )
    return sorted(result, key=lambda segment: (segment["start"], segment["end"]))


def _transcript_result(
    segments: list[dict[str, Any]],
    *,
    text: str = "",
    duration: float | None = None,
    source: str = "youtube_captions",
    title: str = "",
) -> dict[str, Any]:
    end = max((segment["end"] for segment in segments), default=0.0)
    return {
        "title": title,
        "transcript": _clean_text(text)
        or " ".join(segment["text"] for segment in segments),
        "segments": segments,
        "duration": float(max(duration or 0.0, end)),
        "source": source,
    }


def _read_caption_cache(video_id: str) -> dict[str, Any] | None:
    if not _YT_ID.fullmatch(video_id):
        raise InvalidVideoId(video_id)
    try:
        cached = json.loads(
            (_CAPTION_CACHE_DIR / f"{video_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("no_captions"):
        return cached
    # Old plain-text caches cannot supply alignment timestamps: refresh them.
    segments = _segments(cached.get("segments"))
    hosted = cached.get("source") == "serpapi_captions"
    if not segments and not (
        hosted
        and isinstance(cached.get("transcript"), str)
        and cached["transcript"].strip()
    ):
        return None
    result = _transcript_result(
        segments,
        text=str(cached.get("transcript") or cached.get("text") or ""),
        duration=_number(cached.get("duration", cached.get("end"))),
        source=str(cached.get("source") or "youtube_captions"),
        title=str(cached.get("title") or ""),
    )
    if hosted:
        warnings = cached.get("warnings")
        known_duration = _number(cached.get("duration"))
        result.update(
            transcript=cached["transcript"],
            duration=known_duration,
            duration_known=known_duration is not None
            and cached.get("duration_known") is True,
            duration_hint_rejected=cached.get("duration_hint_rejected") is True,
            coverage="partial",
            timing_basis=str(cached.get("timing_basis") or "caption_cue_windows"),
            warnings=[value for value in warnings if isinstance(value, str)]
            if isinstance(warnings, list)
            else [],
        )
    return result


def _write_caption_cache(cache_file: Path, data: dict[str, Any]) -> None:
    temporary = cache_file.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, cache_file)
    except OSError:
        logger.info("Caption cache could not be saved.")
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_hosted_captions(
    video_id: str, *, known_duration: float | None = None
) -> dict[str, Any] | None:
    try:
        return fetch_caption_transcript(video_id, known_duration=known_duration)
    except CaptionProviderError:
        logger.info(
            "Hosted caption provider unavailable; retaining the Whisper fallback."
        )
        return None


def _fetch_youtube_captions_sync(
    video_id: str, *, known_duration: float | None = None
) -> dict[str, Any]:
    cached = _read_caption_cache(video_id)
    cache_file = _CAPTION_CACHE_DIR / f"{video_id}.json"
    if cached is not None:
        if cached.get("no_captions"):
            # This negative cache describes the native providers, not the hosted one.
            hosted = _fetch_hosted_captions(video_id, known_duration=known_duration)
            if hosted is not None:
                _write_caption_cache(cache_file, hosted)
                return hosted
            raise CachedNoCaptions(video_id)
        return cached
    try:
        result = _fetch_youtube_captions_uncached(video_id)
        if not result.get("segments"):
            raise NoCaptionsViaYtDlp(video_id)
    except Exception as exc:
        # Independent hosted captions precede the original audio fallback.
        hosted = _fetch_hosted_captions(video_id, known_duration=known_duration)
        if hosted is not None:
            _write_caption_cache(cache_file, hosted)
            return hosted
        if isinstance(exc, _PERMANENT_CAPTION_ERRORS):
            _write_caption_cache(cache_file, {"no_captions": type(exc).__name__})
        raise
    _write_caption_cache(cache_file, result)
    return result


def _fetch_youtube_captions_uncached(video_id: str) -> dict[str, Any]:
    global _transcript_api_blocked_until, _ytdlp_captions_blocked_until
    blocked = RequestBlocked(video_id)
    permanent: CouldNotRetrieveTranscript | None = None
    if time.monotonic() >= _transcript_api_blocked_until:
        try:
            return _guarded_call("captions_api", {"video_id": video_id})
        except RequestBlocked as exc:
            _transcript_api_blocked_until = time.monotonic() + _BLOCK_BACKOFF_S
            blocked = exc
            logger.info(
                "YouTube caption API blocked; pausing that provider for 30 minutes."
            )
        except _PERMANENT_CAPTION_ERRORS as exc:
            permanent = exc
        except Exception:  # noqa: BLE001 - provider failure must try the independent caption fallback.
            logger.info(
                "Caption API unavailable; trying the alternate caption provider."
            )
    if time.monotonic() >= _ytdlp_captions_blocked_until:
        try:
            return _guarded_call("captions_ytdlp", {"video_id": video_id})
        except RequestBlocked as exc:
            _ytdlp_captions_blocked_until = time.monotonic() + _BLOCK_BACKOFF_S
            blocked = exc
            logger.info(
                "YouTube subtitle download blocked; pausing that provider for 30 minutes."
            )
        except _PERMANENT_CAPTION_ERRORS:
            if permanent is not None:
                raise
        except Exception:  # noqa: BLE001 - transient provider failures must not poison the disk cache.
            logger.info("Alternate caption provider unavailable.")
    raise blocked


def _fetch_with_transcript_api(video_id: str) -> dict[str, Any]:
    transcripts = YouTubeTranscriptApi().list(video_id)
    try:
        transcript = transcripts.find_transcript(_CAPTION_LANGS)
    except NoTranscriptFound:
        available = sorted(transcripts, key=lambda item: item.is_generated)
        if not available:
            raise
        transcript = available[0]
    snippets = list(transcript.fetch())
    segments = _segments(
        [
            {
                "text": snippet.text,
                "start": snippet.start,
                "end": snippet.start + snippet.duration,
            }
            for snippet in snippets
        ]
    )
    if not segments:
        raise NoCaptionsViaYtDlp(video_id)
    return _transcript_result(segments)


def _pick_ytdlp_track(info: dict[str, Any]) -> str | None:
    manual, auto = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    order = [manual.get(lang) for lang in _CAPTION_LANGS]
    order += [tracks for lang, tracks in manual.items() if lang.startswith("en")]
    order += [tracks for lang, tracks in auto.items() if lang.endswith("-orig")]
    order += [auto.get(lang) for lang in _CAPTION_LANGS]
    order += list(manual.values())
    for tracks in order:
        for track in tracks or []:
            if track.get("ext") == "json3" and track.get("url"):
                return track["url"]
    return None


def _fetch_with_ytdlp(video_id: str) -> dict[str, Any]:
    import yt_dlp

    options = _build_ydl_opts(tempfile.gettempdir(), "audio")
    options.update(skip_download=True, ignore_no_formats_error=True)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
        caption_url = _pick_ytdlp_track(info or {})
        if caption_url is None:
            raise NoCaptionsViaYtDlp(video_id)
        data = json.loads(ydl.urlopen(caption_url).read().decode("utf-8"))
    values = []
    for event in data.get("events") or []:
        start, duration = (
            _number(event.get("tStartMs")),
            _number(event.get("dDurationMs")),
        )
        if start is None or duration is None or duration <= 0:
            continue
        values.append(
            {
                "text": "".join(
                    segment.get("utf8", "") for segment in event.get("segs") or []
                ),
                "start": start / 1000,
                "end": (start + duration) / 1000,
            }
        )
    segments = _segments(values)
    if not segments:
        raise NoCaptionsViaYtDlp(video_id)
    return _transcript_result(
        segments,
        duration=_number((info or {}).get("duration")),
        title=str((info or {}).get("title") or ""),
    )


def _probe_sync(path: Path) -> tuple[bool | None, float | None]:
    ffprobe = _find_tool("ffprobe")
    if ffprobe is None:
        return None, None
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        return None, None
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    duration = data.get("format", {}).get("duration")
    try:
        duration = float(duration)
    except (ValueError, TypeError):
        duration = None
    return any(stream.get("codec_type") == "audio" for stream in streams), _number(
        duration
    )


def _to_compact_audio_sync(source: Path, work_dir: Path) -> Path:
    ffmpeg = _find_tool("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg")
    destination = work_dir / "compact.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            str(destination),
        ],
        capture_output=True,
        check=True,
        timeout=900,
    )
    return destination


def _split_audio_sync(source: Path, work_dir: Path) -> list[Path]:
    ffmpeg = _find_tool("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg")
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            "-f",
            "segment",
            "-segment_time",
            str(_CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            str(work_dir / "chunk_%03d.mp3"),
        ],
        capture_output=True,
        check=True,
        timeout=900,
    )
    return sorted(work_dir.glob("chunk_*.mp3"))


async def _transcribe_with_groq(audio: Path, url: str, platform: str) -> dict[str, Any]:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise TranscriptionError(
            "missing_api_key", "Groq transcription is not configured.", url, platform
        )
    data = await asyncio.to_thread(audio.read_bytes)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise TranscriptionError(
            "file_too_large",
            "Audio exceeds the transcription upload limit.",
            url,
            platform,
        )
    try:
        async with AsyncGroq(
            api_key=key,
            max_retries=3,
            timeout=120,
            http_client=httpx.AsyncClient(verify=ssl.create_default_context()),
        ) as client:
            result = await client.audio.transcriptions.create(
                file=(audio.name, data),
                model=WHISPER_MODEL,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                temperature=0.0,
            )
    except AuthenticationError as exc:
        raise TranscriptionError(
            "auth_failed", "Groq rejected its configured credential.", url, platform
        ) from exc
    except RateLimitError as exc:
        raise TranscriptionError(
            "rate_limited",
            "Groq transcription is temporarily rate limited.",
            url,
            platform,
        ) from exc
    except BadRequestError as exc:
        raise TranscriptionError(
            "invalid_audio",
            "Groq could not process the recording's audio.",
            url,
            platform,
        ) from exc
    except APIConnectionError as exc:
        raise TranscriptionError(
            "network_error",
            "The transcription provider could not be reached.",
            url,
            platform,
        ) from exc
    except APIStatusError as exc:
        raise TranscriptionError(
            "transcription_failed", "The transcription provider failed.", url, platform
        ) from exc
    raw = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    return _transcript_result(
        _segments(raw.get("segments")),
        text=str(raw.get("text") or ""),
        duration=_number(raw.get("duration")),
        source="groq_whisper",
    )


async def _transcribe_file(
    path: Path, url: str, platform: str, work_dir: Path
) -> dict[str, Any]:
    has_audio, duration = await asyncio.to_thread(_probe_sync, path)
    if has_audio is False:
        raise TranscriptionError(
            "no_audio", "The recording has no audio track.", url, platform
        )
    upload = path
    if (
        path.suffix.lower() not in _AUDIO_EXTS
        or path.stat().st_size > _MAX_UPLOAD_BYTES
    ):
        try:
            upload = await asyncio.to_thread(_to_compact_audio_sync, path, work_dir)
        except FileNotFoundError:
            if path.stat().st_size > _MAX_UPLOAD_BYTES:
                raise TranscriptionError(
                    "file_too_large",
                    "FFmpeg is required to prepare this recording.",
                    url,
                    platform,
                ) from None
        except subprocess.SubprocessError as exc:
            raise TranscriptionError(
                "no_audio",
                "The recording's audio could not be extracted.",
                url,
                platform,
            ) from exc
    if upload.stat().st_size <= _MAX_UPLOAD_BYTES and (duration or 0) <= _CHUNK_SECONDS:
        result = await _transcribe_with_groq(upload, url, platform)
        return _transcript_result(
            result["segments"],
            text=result["transcript"],
            duration=duration or result["duration"],
            source="groq_whisper",
        )
    try:
        chunks = await asyncio.to_thread(_split_audio_sync, upload, work_dir)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise TranscriptionError(
            "transcription_failed",
            "FFmpeg could not prepare full-length audio chunks.",
            url,
            platform,
        ) from exc
    if not chunks:
        raise TranscriptionError(
            "no_audio", "The recording has no usable audio.", url, platform
        )
    # Keep at most two requests active so a 90-minute source cannot burst the account quota.
    semaphore = asyncio.Semaphore(2)

    async def one_chunk(index: int, chunk: Path) -> dict[str, Any]:
        async with semaphore:
            result = await _transcribe_with_groq(chunk, url, platform)
        result["segments"] = _segments(
            result["segments"], offset=float(index * _CHUNK_SECONDS)
        )
        return result

    results = await asyncio.gather(
        *(one_chunk(index, chunk) for index, chunk in enumerate(chunks))
    )
    segments = [segment for result in results for segment in result["segments"]]
    return _transcript_result(
        segments,
        text=" ".join(result["transcript"] for result in results),
        duration=duration,
        source="groq_whisper",
    )


async def transcribe_youtube_audio(video_id: str) -> dict[str, Any]:
    cached = _read_caption_cache(video_id)
    if cached is not None and (
        cached.get("segments")
        or (cached.get("source") == "serpapi_captions" and cached.get("transcript"))
    ):
        return cached
    url = f"https://www.youtube.com/watch?v={video_id}"
    if not os.getenv("GROQ_API_KEY"):
        raise TranscriptionError(
            "missing_api_key", "Groq transcription is not configured.", url, "youtube"
        )
    logger.info(
        "Source captions unavailable; running the slower full-audio Whisper workaround."
    )
    with tempfile.TemporaryDirectory(prefix="quantro-whisper-") as temporary:
        work_dir = Path(temporary)
        try:
            download = await download_short_media(url, mode="audio", dest_dir=work_dir)
        except MediaDownloadError as exc:
            raise TranscriptionError(exc.code, exc.message, url, "youtube") from exc
        duration = _number(download.get("duration"))
        if duration is None:
            _, duration = await asyncio.to_thread(
                _probe_sync, Path(download["file_path"])
            )
        if duration is not None and duration > 90 * 60:
            raise TranscriptionError(
                "file_too_large",
                "Source recordings over 90 minutes are skipped.",
                url,
                "youtube",
            )
        result = await _transcribe_file(
            Path(download["file_path"]), url, "youtube", work_dir
        )
        result["title"] = str(download.get("title") or "")
        result["duration"] = max(result["duration"], duration or 0.0)
    if result["segments"]:
        _write_caption_cache(_CAPTION_CACHE_DIR / f"{video_id}.json", result)
    return result


async def get_video_transcript(
    url: str,
    media_file_path: str | None = None,
    *,
    known_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        platform = detect_platform(url)
    except MediaDownloadError as exc:
        raise TranscriptionError(exc.code, exc.message, exc.url, exc.platform) from exc
    local_file = Path(media_file_path) if media_file_path is not None else None
    if local_file is not None and not local_file.is_file():
        raise TranscriptionError(
            "file_not_found", "The recording is not available locally.", url, platform
        )
    metadata = dict(known_info or {})
    video_id = extract_youtube_id(url) if platform == "youtube" else None
    if video_id:
        captions_blocked = False
        try:
            known_duration = _number(metadata.get("duration"))
            if known_duration:
                result = await asyncio.to_thread(
                    _fetch_youtube_captions_sync,
                    video_id,
                    known_duration=known_duration,
                )
            else:
                result = await asyncio.to_thread(_fetch_youtube_captions_sync, video_id)
            if result.get("segments") or (
                result.get("source") == "serpapi_captions" and result.get("transcript")
            ):
                result = dict(result)
                result["title"] = str(
                    metadata.get("title") or result.get("title") or ""
                )
                if result.get("source") == "serpapi_captions":
                    result["duration"] = (
                        None
                        if result.get("duration_hint_rejected") is True
                        else known_duration or _number(result.get("duration"))
                    )
                    result["duration_known"] = result["duration"] is not None
                else:
                    result["duration"] = max(
                        _number(result["duration"]) or 0.0, known_duration or 0.0
                    )
                return result
        except Exception as exc:  # noqa: BLE001 - captions are optional and Whisper is the required fallback.
            captions_blocked = isinstance(exc, RequestBlocked)
            logger.info("YouTube captions unavailable; trying Whisper.")
        if local_file is None:
            try:
                return await transcribe_youtube_audio(video_id)
            except TranscriptionError as exc:
                if captions_blocked and exc.code in {
                    "auth_required",
                    "download_failed",
                    "format_unavailable",
                }:
                    exc.platform_challenge = True
                raise
    if not os.getenv("GROQ_API_KEY"):
        raise TranscriptionError(
            "missing_api_key",
            "No captions were available and Groq transcription is not configured.",
            url,
            platform,
        )
    with tempfile.TemporaryDirectory(prefix="quantro-transcribe-") as temporary:
        work_dir = Path(temporary)
        if local_file is None:
            try:
                download = await download_short_media(
                    url, mode="audio", dest_dir=work_dir
                )
            except MediaDownloadError as exc:
                raise TranscriptionError(exc.code, exc.message, url, platform) from exc
            local_file = Path(download["file_path"])
            metadata = metadata or download
        result = await _transcribe_file(local_file, url, platform, work_dir)
    result["title"] = str(metadata.get("title") or "")
    result["duration"] = max(
        result["duration"], _number(metadata.get("duration")) or 0.0
    )
    return result
