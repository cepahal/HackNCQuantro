from math import isfinite


def _number(value, name):
    """Require a finite numeric value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")

    if not isfinite(value):
        raise ValueError(f"{name} must be finite.")

    return float(value)


def _merge_ranges(ranges):
    """Merge overlapping or touching (start, end) intervals."""
    merged = []

    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return merged


def _coverage(start, end, processed_ranges):
    """Measure processed coverage, including processed silent sections."""
    intersections = []

    for item in processed_ranges:
        left = max(start, item["start_seconds"])
        right = min(end, item["end_seconds"])

        if left < right:
            intersections.append((left, right))

    covered_seconds = sum(
        right - left for left, right in _merge_ranges(intersections)
    )

    if covered_seconds == 0:
        return "unavailable"

    if covered_seconds >= (end - start) - 1e-6:
        return "complete"

    return "partial"


def select_surrounding_context(alignment, candidate_features, options=None):
    """Return transcript windows surrounding verified source matches."""
    if options is None:
        options = {}

    if alignment["status"] != "verified":
        raise ValueError("Context selection requires a verified alignment.")

    source_media_id = candidate_features["media_id"]

    if alignment["source_media_id"] != source_media_id:
        raise ValueError("Alignment and candidate media IDs do not match.")

    duration = _number(
        candidate_features["duration_seconds"], "duration_seconds"
    )
    padding = _number(
        options.get("padding_seconds", 30.0), "padding_seconds"
    )
    merge_windows = options.get("merge_overlapping_windows", True)

    if duration <= 0:
        raise ValueError("Source duration must be greater than zero.")

    if padding < 0:
        raise ValueError("Padding cannot be negative.")

    if not isinstance(merge_windows, bool):
        raise ValueError("merge_overlapping_windows must be a boolean.")

    matches = alignment["matches"]

    if not matches:
        raise ValueError("A verified alignment must contain a match.")

    # Build padded windows using SOURCE timestamps.
    windows = []

    for match in matches:
        start = _number(match["source_start_seconds"], "source_start_seconds")
        end = _number(match["source_end_seconds"], "source_end_seconds")

        if not 0 <= start < end <= duration:
            raise ValueError("A source match falls outside the recording.")

        windows.append({
            "start": max(0.0, start - padding),
            "end": min(duration, end + padding),
            "match_ids": [match["match_id"]],
        })

    windows.sort(key=lambda window: window["start"])

    if merge_windows:
        merged = []

        for window in windows:
            if merged and window["start"] <= merged[-1]["end"]:
                previous = merged[-1]
                previous["end"] = max(previous["end"], window["end"])

                for match_id in window["match_ids"]:
                    if match_id not in previous["match_ids"]:
                        previous["match_ids"].append(match_id)
            else:
                merged.append(window)

        windows = merged

    transcript = candidate_features["transcript"]
    processed_ranges = transcript["processed_ranges"]
    for interval in processed_ranges:
        left = _number(interval["start_seconds"], "processed start")
        right = _number(interval["end_seconds"], "processed end")
        if not 0 <= left < right <= duration:
            raise ValueError("Processed transcript range is outside the recording.")
    sentences = transcript["sentences"]

    result_windows = []
    limitations = list(candidate_features.get("warnings", []))

    for index, window in enumerate(windows, start=1):
        start = window["start"]
        end = window["end"]
        context_id = f"context_{index:03d}"

        # Keep whole sentences that overlap the window.
        # Preserve their original timestamps and all other fields.
        selected_sentences = [
            dict(sentence)
            for sentence in sentences
            if sentence["start_seconds"] < end
            and sentence["end_seconds"] > start
        ]
        selected_sentences.sort(
            key=lambda sentence: (
                sentence["start_seconds"],
                sentence["end_seconds"],
            )
        )

        coverage = _coverage(start, end, processed_ranges)

        result_windows.append({
            "context_id": context_id,
            "match_ids": list(window["match_ids"]),
            "requested_start_seconds": start,
            "requested_end_seconds": end,
            "coverage": coverage,
            "sentences": selected_sentences,
        })

        if coverage != "complete":
            limitations.append(
                f"{context_id}: transcript coverage is {coverage}; "
                "unprocessed intervals cannot be treated as silence."
            )

    return {
        "source_media_id": source_media_id,
        "windows": result_windows,
        "limitations": limitations,
    }