"""Per-job public diagnostic snapshots; never intercept stdout or provider logs."""

import json
import math
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_CAPTURE: ContextVar["DiagnosticCapture | None"] = ContextVar("resolver_diagnostics", default=None)
_KEYS = frozenset(["url", "title", "platform", "duration", "duration_seconds", "transcript_source", "full_transcript", "downloaded", "post", "report", "media_id", "post_id", "candidate_id", "post_url", "caption", "author", "engagement", "published_at", "media", "retrieval", "id", "handle", "display_name", "profile_url", "bio", "verified", "followers_count", "following_count", "posts_count", "views_count", "likes_count", "comments_count", "shares_count", "mime_type", "playback_url", "width", "height", "fps", "has_audio", "retrieved_at", "provider", "metadata_source_urls", "unavailable_fields", "warnings", "language", "transcript", "status", "processed_ranges", "sentences", "sentence_id", "start_seconds", "end_seconds", "text", "speaker_id", "words", "frames", "frame_id", "timestamp_seconds", "image_url", "visual_text", "speaker_and_event", "unique_phrase", "query", "title_query", "queries", "returned_count", "kept_count", "video_id", "video_url", "match_score", "transcript_result", "source", "segments", "start", "end", "coverage", "duration_known", "duration_hint_rejected", "timings", "created_at", "ok", "reason", "selected", "candidate_number", "candidate_count", "candidates", "leads", "source_match", "search_metadata", "threshold", "alignment", "clip_media_id", "source_media_id", "matches", "match_id", "clip_start_seconds", "clip_end_seconds", "source_start_seconds", "source_end_seconds", "evidence", "method", "clip_refs", "source_refs", "description", "match_quality", "confidence", "level", "basis", "limitations", "matched_clip_seconds", "clip_coverage_ratio", "metrics", "name", "value", "unit", "methods_used", "edit_analysis", "scope", "minimum_gap_seconds", "source_gaps", "before_match_id", "after_match_id", "unmatched_clip_ranges", "counts", "matched_passages", "unmatched_clip_segments", "confirmed_added_segments", "confirmed_deleted_segments", "context", "windows", "context_id", "match_ids", "requested_start_seconds", "requested_end_seconds", "comparison", "context_assessment", "caption_assessment", "assessment_confidence", "summary", "findings", "finding_id", "type", "explanation", "sentence_ids", "quote", "submitted", "schema_version", "job_id", "outcome", "report_id", "code", "stage", "retryable", "delivery_status", "message"])
_PRIVATE_KEY = re.compile(r"(?:secret|token|password|credential|api.?key|authorization|cookie|(?:^|_)path$|^file$)", re.IGNORECASE)
_PRIVATE_VALUE = re.compile(r"file://|(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?:/tmp/|/home/|/Users/)|\\\\[^\s\\]+\\", re.IGNORECASE)
_CREDENTIAL = re.compile(r"\b(?:bearer\s+\S+|(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*[^\s,;]+)", re.IGNORECASE)
_KEYS = _KEYS | frozenset({"context_video", "context_selection", "strategy", "before", "after", "before_method", "after_method", "timing_note"})
_KEYS = _KEYS | frozenset({"exception_type", "evidence_hash", "interpretation", "context_gap", "reliability", "references", "model", "generated_at"})


def safe_url(value: str) -> str | None:
    """Retain public location only, dropping userinfo, fragments and signed queries."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower()
        if host in {"localhost", "127.0.0.1", "::1"} or "." not in host:
            return None
        query = ""
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            identity = parse_qs(parts.query).get("v", [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
                query = urlencode({"v": identity})
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    except ValueError:
        return None


def public_data(value: Any, key: str = "") -> Any:
    """Project explicit analysis fields; unrecognized/private fields never persist."""
    if isinstance(value, dict):
        return {name: public_data(item, name) for name, item in value.items()
                if isinstance(name, str) and name in _KEYS and not _PRIVATE_KEY.search(name)}
    if isinstance(value, (list, tuple)):
        if key == "unavailable_fields":
            value = [item for item in value if not isinstance(item, str) or not _PRIVATE_KEY.search(item.rsplit(".", 1)[-1])]
        return [public_data(item, key) for item in value]
    if isinstance(value, str):
        if _PRIVATE_VALUE.search(value):
            return "[private reference omitted]"
        if key.endswith("url") or key == "url" or key == "metadata_source_urls":
            return safe_url(value)
        return _CREDENTIAL.sub("[credential omitted]", value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (float, int)):
        return value if math.isfinite(value) else None
    return None


def public_events(events: Any) -> list[dict[str, Any]]:
    result = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        elapsed = event.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(elapsed):
            continue
        result.append({"stage": public_data(str(event.get("stage", ""))),
                       "status": public_data(str(event.get("status", ""))),
                       "elapsed_seconds": max(0.0, elapsed),
                       "data": public_data(event.get("data", {}))})
        if event.get("timing_kind") in ("stage_duration", "job_elapsed"):
            result[-1]["timing_kind"] = event["timing_kind"]
    return result


def diagnostic_response(job_id: str, events: Any) -> dict[str, Any]:
    safe = public_events(events)
    return {"job_id": job_id, "capture_status": "recorded" if safe else "unavailable",
            "events": safe,
            "terminal_output": "\n\n".join(
                f"[{event['stage']}] {event['status']} ({event['elapsed_seconds']:.3f}s)\n"
                + json.dumps(event["data"], ensure_ascii=False, indent=2) for event in safe)}


class DiagnosticCapture:
    def __init__(self, job_id: str, store: Any) -> None:
        self.job_id, self.store = job_id, store
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def record(self, stage: str, status: str, data: dict[str, Any], *, started: float | None = None) -> None:
        """Capture failures must never change the core analysis result."""
        try:
            self.events.append({"stage": stage, "status": status,
                                "timing_kind": "stage_duration" if started is not None else "job_elapsed",
                                "elapsed_seconds": round(time.monotonic() - (started if started is not None else self.started), 6),
                                "data": public_data(data)})
            self.store.save_diagnostics(self.job_id, self.events)
        except Exception:  # noqa: BLE001, S110 - Optional capture must never interrupt analysis or log private data.
            pass

    @contextmanager
    def bind(self) -> Iterator["DiagnosticCapture"]:
        token = _CAPTURE.set(self)
        try:
            yield self
        finally:
            _CAPTURE.reset(token)


def record(stage: str, status: str, data: dict[str, Any], *, started: float | None = None) -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        capture.record(stage, status, data, started=started)


def media_snapshot(post: Any, features: Any) -> dict[str, Any]:
    return {"post_url": post.post_url, "downloaded": bool(post.media.local_path),
            "media_id": post.media.media_id, "title": post.title, "platform": post.platform,
            "duration_seconds": features.duration_seconds, "transcript_source": post.retrieval.provider,
            "full_transcript": getattr(features, "full_transcript_text", None) or " ".join(s.text for s in features.transcript.sentences),
            "post": post.model_dump(mode="json"), "transcript": features.transcript.model_dump(mode="json"),
            "frames": [frame.model_dump(mode="json") for frame in features.frames],
            "visual_text": [entry.model_dump(mode="json") for entry in features.visual_text],
            "warnings": features.warnings}
