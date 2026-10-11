import pytest
from services.resolution import transcribe


def test_word_timestamps_survive_offsets():
    segment = {
        "text": "Hello world.",
        "start": 1,
        "end": 4,
        "words": [
            {"word": "Hello", "start": 1.2, "end": 2},
            {"word": "world.", "start": 2.3, "end": 3.5},
        ],
    }
    result = transcribe._segments([segment], offset=1200)[0]
    assert result["words"] == [
        {"word": "Hello", "start": 1201.2, "end": 1202},
        {"word": "world.", "start": 1202.3, "end": 1203.5},
    ]
    assert transcribe._segments([result]) == [result]


def test_flat_provider_words_assigned_by_actual_bounds():
    segments = [
        {"text": "Hello", "start": 1, "end": 2},
        {"text": "World", "start": 3, "end": 4},
    ]
    words = [
        {"word": "Hello", "start": 1.1, "end": 1.9},
        {"word": "World", "start": 3.1, "end": 3.9},
    ]
    result = transcribe._segments(segments, words=words)
    assert result[0]["words"] == [words[0]]
    assert result[1]["words"] == [words[1]]


@pytest.mark.parametrize(
    "words",
    [
        [{"word": "Hello", "start": 1, "end": 2}],
        [{"word": "Hello world", "start": float("nan"), "end": 3}],
        [{"word": "Hello world", "start": -1, "end": 3}],
        [{"word": "Hello world", "start": 1, "end": 5}],
        [
            {"word": "Hello", "start": 2, "end": 3},
            {"word": "world", "start": 1, "end": 2},
        ],
    ],
)
def test_invalid_or_incomplete_words_fall_back_to_segment(words):
    result = transcribe._segments(
        [{"text": "Hello world", "start": 1, "end": 4, "words": words}]
    )
    assert result == [{"text": "Hello world", "start": 1.0, "end": 4.0}]
