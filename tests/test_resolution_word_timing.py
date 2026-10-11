"""Supplied provider words survive resolver conversion without invented timing."""

import asyncio
from copy import deepcopy

import pytest

from backend.services.resolution.alignment import _timed_text
from backend.services.resolution.media import resolve_media

URL = "https://www.youtube.com/watch?v=3XPuGqqWLtk"
WORDS = [
    {"text": "No", "start_seconds": 10.25, "end_seconds": 10.75, "confidence": 0.91},
    {"text": "policy", "start_seconds": 11.0, "end_seconds": 11.7, "probability": 0.83},
    {"text": "changes", "start_seconds": 12.0, "end_seconds": 12.9},
    {"text": "today.", "start_seconds": 13.0, "end_seconds": 13.75},
]


def resolve(words):
    segment = {"text": "No policy changes today.", "start": 10.0, "end": 14.0}
    if words is not None:
        segment["words"] = words
    payload = {"transcript": segment["text"], "segments": [segment], "duration": 300.0}
    _post, features = asyncio.run(
        resolve_media(
            URL,
            candidate_id="candidate_1",
            transcript_result=payload,
            metadata={"duration": 300.0},
        )
    )
    return features


def test_real_supplied_words_and_confidences_survive_conversion():
    raw = deepcopy(WORDS)
    features = resolve(raw)
    words = features.transcript.sentences[0].words
    assert [w.model_dump() for w in words] == [
        {key: item[key] for key in ("text", "start_seconds", "end_seconds")}
        for item in WORDS
    ]
    assert features.word_confidences == {
        "s1": [{"confidence": 0.91}, {"probability": 0.83}, {}, {}]
    }
    assert "word_confidences" not in features.model_dump()
    assert features.model_dump()["transcript"]["sentences"][0]["words"] == [
        w.model_dump() for w in words
    ]
    assert raw == WORDS
    _, spans = _timed_text(features)
    assert [(span.start, span.end) for span in spans] == [
        (w["start_seconds"], w["end_seconds"]) for w in WORDS
    ]


def test_provider_word_start_end_aliases_preserve_bounds():
    words = [
        {"word": w["text"], "start": w["start_seconds"], "end": w["end_seconds"]}
        for w in WORDS
    ]
    observed = resolve(words).transcript.sentences[0].words
    assert observed[0].start_seconds == 10.25
    assert observed[-1].end_seconds == 13.75


@pytest.mark.parametrize(
    "mutation",
    [
        "nan",
        "inf",
        "bool",
        "string_time",
        "backwards",
        "overlap",
        "before_segment",
        "after_segment",
        "zero_length",
        "missing_word",
        "wrong_text",
        "blank",
        "not_mapping",
        "wrong_container",
    ],
)
def test_invalid_or_incomplete_words_fall_back_without_losing_segment(mutation):
    words = deepcopy(WORDS)
    if mutation in ("nan", "inf", "bool", "string_time"):
        words[0]["start_seconds"] = {
            "nan": float("nan"),
            "inf": float("inf"),
            "bool": True,
            "string_time": "10.25",
        }[mutation]
    elif mutation == "backwards":
        words[0]["end_seconds"] = 10.0
    elif mutation == "overlap":
        words[1]["start_seconds"] = 10.5
    elif mutation == "before_segment":
        words[0]["start_seconds"] = 9.0
    elif mutation == "after_segment":
        words[-1]["end_seconds"] = 15.0
    elif mutation == "zero_length":
        words[0]["end_seconds"] = words[0]["start_seconds"]
    elif mutation == "missing_word":
        words.pop()
    elif mutation == "wrong_text":
        words[0]["text"] = "Yes"
    elif mutation == "blank":
        words[0]["text"] = " "
    elif mutation == "not_mapping":
        words[0] = "No"
    elif mutation == "wrong_container":
        words = {"unexpected": WORDS}
    features = resolve(words)
    sentence = features.transcript.sentences[0]
    assert sentence.words == []
    assert sentence.text == "No policy changes today."
    assert (sentence.start_seconds, sentence.end_seconds) == (10.0, 14.0)
    assert features.word_confidences == {}
    assert any("word timestamps were ignored" in w for w in features.warnings)


def test_no_word_times_remains_legacy_segment_only():
    features = resolve(None)
    assert features.transcript.sentences[0].words == []
    assert "words" not in features.model_dump()["transcript"]["sentences"][0]
    assert not any("word timestamps were ignored" in w for w in features.warnings)


def test_invalid_confidence_does_not_invalidate_real_word_times():
    words = deepcopy(WORDS)
    words[0]["confidence"] = float("nan")
    words[1]["probability"] = 1.5
    features = resolve(words)
    assert len(features.transcript.sentences[0].words) == 4
    assert features.word_confidences == {"s1": [{}, {}, {}, {}]}
