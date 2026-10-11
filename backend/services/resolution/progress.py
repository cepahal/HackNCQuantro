"""Small allowlisted view of persisted events for the personalized loading UI."""

import math
import re

from .diagnostics import public_data, public_events


def _text(value, limit=240):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _score(value):
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
        else None
    )


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _video_ready(value):
    asset = _mapping(value)
    return (
        asset.get("mime_type") == "video/mp4"
        and isinstance(asset.get("media_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", asset["media_id"]) is not None
    )


def _accepted_match(report):
    alignment = _mapping(report.get("alignment"))
    clip = _mapping(
        _mapping(_mapping(report.get("submitted")).get("post")).get("media")
    ).get("media_id")
    source = _mapping(
        _mapping(_mapping(report.get("source")).get("post")).get("media")
    ).get("media_id")
    confidence = _mapping(_mapping(alignment.get("match_quality")).get("confidence"))
    return (
        report.get("outcome") == "matched"
        and isinstance(clip, str)
        and bool(clip)
        and isinstance(source, str)
        and bool(source)
        and source != clip
        and alignment.get("status") == "verified"
        and alignment.get("clip_media_id") == clip
        and alignment.get("source_media_id") == source
        and confidence.get("level") in {"high", "moderate"}
        and any(
            isinstance(row, dict)
            and type(row.get("source_start_seconds")) in (int, float)
            and type(row.get("source_end_seconds")) in (int, float)
            and math.isfinite(row["source_start_seconds"])
            and math.isfinite(row["source_end_seconds"])
            and 0 <= row["source_start_seconds"] < row["source_end_seconds"]
            for row in _rows(alignment.get("matches"))
        )
    )


def progress_snapshot(job_id, job, events, report=None):
    result = {
        "job_id": job_id,
        "stage": "retrieve",
        "status": job.get("status")
        if job.get("status")
        in {"running", "ingested", "completed", "failed", "pending"}
        else "running",
        "title": "",
        "excerpt": "",
        "speaker_clue": "",
        "anchor": "",
        "candidates": [],
        "source_title": "",
        "source_start": None,
        "source_end": None,
        "before": [],
        "after": [],
        "context_method": "",
        "video_ready": False,
        "message": "",
    }
    candidates = {}
    for event in public_events(events):
        stage, status, data = event["stage"], event["status"], event["data"]
        if not isinstance(data, dict):
            continue
        if stage == "retrieve_post" and status == "completed":
            result.update(
                stage="transcript",
                title=_text(data.get("title"), 120),
                excerpt=_text(data.get("full_transcript")),
            )
        if stage == "build_search_queries" and status == "completed":
            result.update(
                stage="search",
                speaker_clue=_text(data.get("speaker_and_event"), 120),
                anchor=_text(data.get("unique_phrase"), 160),
            )
        if stage in {"search_query", "search_candidates"}:
            result["stage"] = "candidates"
        if stage == "candidate_transcript" and status == "scored":
            key = _text(data.get("video_id"), 64)
            if key and (key in candidates or len(candidates) < 3):
                candidates[key] = {
                    "title": _text(data.get("title"), 120),
                    "score": _score(data.get("match_score")),
                }
        if stage == "candidate_evaluation" and status == "eligible":
            result["stage"] = "align"
        if stage == "select_source" and status == "completed":
            result["stage"] = "align" if data.get("selected") else "candidates"
        if stage == "selected_passage" and status == "completed":
            result.update(stage="align", source_title=_text(data.get("title"), 120))
            matches = _rows(data.get("matches"))
            if matches and isinstance(matches[0], dict):
                a, b = (
                    matches[0].get("source_start_seconds"),
                    matches[0].get("source_end_seconds"),
                )
                if (
                    type(a) in (int, float)
                    and type(b) in (int, float)
                    and math.isfinite(a)
                    and math.isfinite(b)
                    and 0 <= a < b
                ):
                    result.update(source_start=a, source_end=b)
        if stage == "sentence_context" and status == "completed":
            result.update(
                stage="context", context_method=_text(data.get("strategy"), 60)
            )
            for side in ("before", "after"):
                rows = data.get(side)
                if isinstance(rows, list):
                    result[side] = [
                        _text(s.get("text"))
                        for s in rows[:3]
                        if isinstance(s, dict) and _text(s.get("text"))
                    ]
        if stage == "context_video":
            result.update(
                stage="export", video_ready=status == "completed" and _video_ready(data)
            )
            if status == "unavailable":
                result["message"] = (
                    "The context video could not be exported. Analysis may continue."
                )
    result["candidates"] = list(candidates.values())
    if isinstance(report, dict):
        report = public_data(report)
        source = _mapping(report.get("source"))
        result["source_title"] = _text(_mapping(source.get("post")).get("title"), 120)
        result.update(source_start=None, source_end=None)
        result["title"] = (
            _text(
                _mapping(_mapping(report.get("submitted")).get("post")).get("title"),
                120,
            )
            or result["title"]
        )
        submitted = _mapping(report.get("submitted"))
        transcript = _mapping(submitted.get("transcript"))
        result["excerpt"] = result["excerpt"] or _text(
            " ".join(
                _text(row.get("text"))
                for row in _rows(transcript.get("sentences"))
                if isinstance(row, dict)
            )
        )
        selection = _mapping(report.get("context_selection"))
        result["context_method"] = _text(selection.get("strategy"), 60)
        for side in ("before", "after"):
            result[side] = [
                _text(row.get("text"))
                for row in _rows(selection.get(side))[:3]
                if isinstance(row, dict)
            ]
        result["video_ready"] = _video_ready(report.get("context_video"))
        matches = _rows(_mapping(report.get("alignment")).get("matches"))
        valid = [
            m
            for m in matches
            if isinstance(m, dict)
            and type(m.get("source_start_seconds")) in (int, float)
            and type(m.get("source_end_seconds")) in (int, float)
            and math.isfinite(m["source_start_seconds"])
            and math.isfinite(m["source_end_seconds"])
            and 0 <= m["source_start_seconds"] < m["source_end_seconds"]
        ]
        if valid:
            result.update(
                source_start=min(m["source_start_seconds"] for m in valid),
                source_end=max(m["source_end_seconds"] for m in valid),
            )
    if job.get("status") == "completed":
        if (
            not isinstance(report, dict)
            or report.get("job_id", job_id) != job_id
            or (
                report.get("outcome") != "no_verified_match"
                and not _accepted_match(report)
            )
        ):
            result.update(
                stage="failed",
                video_ready=False,
                source_title="",
                source_start=None,
                source_end=None,
                before=[],
                after=[],
                context_method="",
                message="The saved report is unavailable. Retry loading the report.",
            )
        elif report and report.get("outcome") == "no_verified_match":
            result.update(
                stage="no_match",
                message="No source passed the matching checks.",
                video_ready=False,
                source_title="",
                source_start=None,
                source_end=None,
                before=[],
                after=[],
                context_method="",
            )
        else:
            result["stage"] = "complete"
    elif job.get("status") == "failed":
        result.update(
            stage="failed",
            video_ready=False,
            message="Analysis stopped. See the report page for the error and retry options.",
        )
    return result
