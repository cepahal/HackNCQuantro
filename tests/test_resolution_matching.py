"""Offline regressions for the supplied source matcher and timed localization."""

from types import SimpleNamespace

import pytest

from backend.services.resolution import candidates
from backend.services.resolution.alignment import build_alignment, build_context
from backend.services.shared.types import MediaFeatures

PASSAGE = (
    "The observatory measured hydrogen emissions from distant galaxies before "
    "the expedition returned with unprecedented photographic evidence."
)
A, B, C, D, SELF = (
    "AAAAAAAAAAA",
    "BBBBBBBBBBB",
    "CCCCCCCCCCC",
    "DDDDDDDDDDD",
    "SSSSSSSSSSS",
)


def features(media_id, sentences, duration=900.0, status="complete", ranges=None):
    return MediaFeatures.model_validate(
        {
            "media_id": media_id,
            "duration_seconds": duration,
            "transcript": {
                "status": status,
                "processed_ranges": ranges
                if ranges is not None
                else [{"start_seconds": 0.0, "end_seconds": duration}],
                "sentences": sentences,
            },
        }
    )


def sentence(sentence_id, text, start, end, **extra):
    return {
        "sentence_id": sentence_id,
        "text": text,
        "start_seconds": float(start),
        "end_seconds": float(end),
        **extra,
    }


def pair(text=PASSAGE):
    clip = features("clip", [sentence("clip_001", text, 0.25, 9.75)], duration=10.0)
    source = features(
        "source",
        [
            sentence(
                "source_before",
                "Opening remarks concern research funding and the telescope schedule.",
                20,
                35,
            ),
            sentence("source_match", PASSAGE, 125.5, 138.75),
            sentence(
                "source_after",
                "The photographic plates are now preserved in the university archive.",
                170,
                185,
            ),
        ],
    )
    return clip, source


def transcript(text, start=120.0, end=135.0):
    return {
        "transcript": text,
        "segments": [{"text": text, "start": start, "end": end}],
        "source": "youtube_captions",
        "duration": 900.0,
        "title": "Observatory expedition",
    }


def test_known_excerpt_uses_real_caption_timestamps_and_references():
    clip, source = pair()
    result = build_alignment(clip, source, 100.0)
    assert result.status == "verified"
    match = result.matches[0]
    assert (match.source_start_seconds, match.source_end_seconds) == (125.5, 138.75)
    assert (match.clip_start_seconds, match.clip_end_seconds) == (0.25, 9.75)
    assert match.evidence[0].source_refs == ["source_match"]
    assert match.evidence[0].clip_refs == ["clip_001"]
    assert result.match_quality.confidence.level == "high"
    assert result.match_quality.methods_used == ["transcript_partial_ratio"]
    assert result.edit_analysis.status == "not_assessed"
    assert result.edit_analysis.counts.confirmed_deleted_segments is None
    assert any("heuristic" in item for item in result.limitations)


def test_fuzzy_asr_difference_can_align_without_fabricating_times():
    clip, source = pair(PASSAGE.replace("hydrogen", "helium"))
    result = build_alignment(clip, source, 94.0)
    assert result.status == "verified"
    assert result.matches[0].source_start_seconds == 125.5
    assert result.matches[0].source_end_seconds == 138.75


def test_word_timestamps_narrow_match_without_interpolation():
    text = "Introductions precede " + PASSAGE + " Closing announcements follow."
    words = [
        {"text": word, "start_seconds": 100.0 + index, "end_seconds": 100.6 + index}
        for index, word in enumerate(text.split())
    ]
    source = features("source", [sentence("source_001", text, 100, 140, words=words)])
    clip, _ = pair()
    result = build_alignment(clip, source, 100.0)
    assert result.status == "verified"
    assert result.matches[0].source_start_seconds == words[2]["start_seconds"]
    assert result.matches[0].source_end_seconds == words[-4]["end_seconds"]


@pytest.mark.parametrize(
    "text",
    [
        "Hydrogen emissions reveal distant galaxies.",
        "Thank you very much everyone and welcome here today to all the great people.",
        "The kittens played beside the kitchen while their owners prepared dinner for visiting relatives.",
    ],
)
def test_tiny_generic_and_unrelated_speech_do_not_verify(text):
    clip, source = pair(text)
    assert build_alignment(clip, source, 100.0).status == "no_verified_match"


def test_score_below_threshold_cannot_verify_an_exact_passage():
    clip, source = pair()
    assert build_alignment(clip, source, 74.9).status == "no_verified_match"
    assert (
        build_alignment(clip, source, 75.0).match_quality.confidence.level == "moderate"
    )


def test_repeated_source_passage_has_no_arbitrary_timestamp():
    clip, source = pair()
    source.transcript.sentences.append(
        type(source.transcript.sentences[0]).model_validate(
            sentence("source_repeat", PASSAGE, 400, 415)
        )
    )
    result = build_alignment(clip, source, 100.0)
    assert result.status == "no_verified_match"
    assert result.matches == []
    assert any("ambiguous" in item for item in result.limitations)


def test_self_source_cannot_verify_itself():
    clip, _ = pair()
    assert build_alignment(clip, clip, 100.0).status == "no_verified_match"


def test_untimed_or_invalid_timing_cannot_be_filled_in_from_text():
    clip, source = pair()
    source.transcript.sentences = []
    assert build_alignment(clip, source, 100.0).status == "not_assessed"
    _, source = pair()
    source.transcript.sentences[1].end_seconds = 901.0
    assert build_alignment(clip, source, 100.0).status == "not_assessed"


def test_tiny_source_substring_is_not_a_match_of_a_long_clip():
    clip, _ = pair()
    source = features("source", [sentence("source_001", "distant galaxies", 20, 22)])
    assert build_alignment(clip, source, 100.0).status == "no_verified_match"


def test_context_keeps_full_segments_and_marks_missing_coverage():
    clip, source = pair()
    source.transcript.sentences.insert(
        1,
        type(source.transcript.sentences[0]).model_validate(
            sentence(
                "source_boundary",
                "Earlier qualifications must remain intact, even across the requested boundary.",
                60,
                70,
            )
        ),
    )
    result = build_alignment(clip, source, 100.0)
    context = build_context(result, source)
    assert context is not None
    window = context.windows[0]
    assert (window.requested_start_seconds, window.requested_end_seconds) == (
        65.5,
        198.75,
    )
    assert [item.sentence_id for item in window.sentences] == [
        "source_boundary",
        "source_match",
        "source_after",
    ]
    assert window.sentences[0].start_seconds == 60.0
    assert window.sentences[0].text == source.transcript.sentences[1].text
    assert window.coverage == "complete"
    source.transcript.processed_ranges[0].start_seconds = 120.0
    assert build_context(result, source).windows[0].coverage == "partial"


def test_context_is_absent_when_alignment_is_not_verified():
    clip, source = pair()
    assert build_context(build_alignment(clip, source, 20.0), source) is None


@pytest.mark.asyncio
async def test_metadata_preserves_clean_title_query_and_heuristic_verbatim_anchor(
    monkeypatch,
):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    result = await candidates.extract_search_metadata(
        PASSAGE, "Observatory expedition 🔭 #science @poster shorts"
    )
    assert result["title_query"] == "observatory expedition"
    assert result["speaker_and_event"] == "observatory expedition"
    assert result["unique_phrase"] in candidates.normalize_text(PASSAGE)
    assert result["query"] != result["title_query"]


@pytest.mark.asyncio
async def test_fabricated_llm_anchor_is_replaced_but_event_is_preserved(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-placeholder")
    received = {}

    class FakeGroq:
        def __init__(self, **kwargs):
            self.http_client = kwargs["http_client"]
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            await self.http_client.aclose()

        async def create(self, **kwargs):
            received.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"speaker_and_event":"Astronomy symposium 2020",'
                                '"unique_phrase":"I invented these seven words for searching"}'
                            )
                        )
                    )
                ]
            )

    monkeypatch.setattr(candidates, "AsyncGroq", FakeGroq)
    result = await candidates.extract_search_metadata(PASSAGE, "Telescope research")
    assert result["speaker_and_event"] == "Astronomy symposium 2020"
    assert result["unique_phrase"] in candidates.normalize_text(PASSAGE)
    assert "invented" not in result["unique_phrase"]
    assert received["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_search_interleaves_deduplicates_and_excludes_self_and_shorts(
    monkeypatch,
):
    async def search(query, _limit):
        return {
            "phrase": [
                candidates.Candidate(SELF, "Self", 900),
                candidates.Candidate("SHORTSHORT1", "Short", 179),
                candidates.Candidate(A, "A", 900),
                candidates.Candidate(C, "C", 180),
            ],
            "title": [
                candidates.Candidate(B, "B", 500),
                candidates.Candidate(A, "A duplicate", 900),
                candidates.Candidate(D, "D", 600),
            ],
        }[query]

    monkeypatch.setattr(candidates, "_search", search)
    meta = {
        "query": "phrase",
        "title_query": "title",
        "speaker_and_event": "event",
        "unique_phrase": "quote",
    }
    result = await candidates.search_candidates(meta, [SELF])
    assert [item.video_id for item in result] == [A, B, C]


@pytest.mark.asyncio
async def test_failed_phrase_search_does_not_suppress_the_working_title_query(
    monkeypatch,
):
    async def search(query, _limit):
        if query == "phrase":
            raise candidates.CandidateSearchError("search_failed", "Offline failure")
        return [candidates.Candidate(B, "Title result", 900)]

    monkeypatch.setattr(candidates, "_search", search)
    meta = {
        "query": "phrase",
        "title_query": "title",
        "speaker_and_event": "event",
        "unique_phrase": "quote",
    }
    assert (await candidates.search_candidates(meta))[0].video_id == B


@pytest.mark.asyncio
async def test_empty_searches_loosen_the_query_and_keep_only_three_unique_leads(
    monkeypatch,
):
    async def search(query, _limit):
        return (
            [
                candidates.Candidate(A, "A", 900),
                candidates.Candidate(A, "duplicate", 900),
                candidates.Candidate(B, "B", 900),
            ]
            if query == "event quote"
            else []
        )

    monkeypatch.setattr(candidates, "_search", search)
    meta = {
        "query": "phrase",
        "title_query": "title",
        "speaker_and_event": "event",
        "unique_phrase": "quote",
    }
    assert [item.video_id for item in await candidates.search_candidates(meta)] == [
        A,
        B,
    ]


@pytest.mark.asyncio
async def test_every_candidate_is_scored_and_ties_keep_search_order_despite_one_failure(
    monkeypatch,
):
    checked = []

    async def metadata(*_):
        return {}

    async def search(*_):
        return [
            candidates.Candidate(A, "Unavailable", 900),
            candidates.Candidate(B, "First match", 900),
            candidates.Candidate(C, "Second match", 900),
        ]

    def captions(video_id):
        checked.append(video_id)
        if video_id == A:
            raise RuntimeError("Offline captions unavailable")
        return transcript("Opening remarks. " + PASSAGE + " Closing remarks.")

    async def whisper(_video_id):
        raise RuntimeError("Offline fallback unavailable")

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "search_candidates", search)
    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    result = await candidates.find_source_video(PASSAGE)
    assert checked == [A, B, C]
    assert result["status"] == "success"
    assert result["video_id"] == B
    assert [item["video_id"] for item in result["candidates"]] == [B, C]
    assert [item["video_id"] for item in result["leads"]] == [A, B, C]
    assert result["warnings"]
    assert result["candidates"][0]["transcript_result"]["segments"][0]["start"] == 120.0


@pytest.mark.asyncio
async def test_candidate_without_captions_uses_timed_whisper_result(monkeypatch):
    def captions(_video_id):
        raise RuntimeError("Offline caption block")

    async def whisper(video_id):
        assert video_id == A
        return {**transcript(PASSAGE), "source": "groq_whisper"}

    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    result = await candidates._score_candidate(
        candidates.Candidate(A, "A", 5400), candidates.normalize_text(PASSAGE)
    )
    assert result["match_score"] == 100.0
    assert result["transcript_result"]["source"] == "groq_whisper"


@pytest.mark.asyncio
async def test_whisper_fallback_skips_recordings_longer_than_ninety_minutes(
    monkeypatch,
):
    def captions(_video_id):
        raise RuntimeError("Offline caption block")

    async def whisper(_video_id):
        pytest.fail("The >90 minute recording must not be downloaded/transcribed")

    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    assert (
        await candidates._score_candidate(
            candidates.Candidate(A, "A", 5401), "clip text"
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_usable_transcripts_returns_not_found_with_null_source(monkeypatch):
    async def metadata(*_):
        return {}

    async def search(*_):
        return []

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "search_candidates", search)
    result = await candidates.find_source_video(PASSAGE)
    assert result == {
        "status": "not_found",
        "match_score": 0.0,
        "video_id": None,
        "video_url": None,
        "title": None,
        "candidates": [],
        "leads": [],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_unknown_duration_is_probed_before_whisper_and_cannot_bypass_limit(
    monkeypatch,
):
    async def metadata(_url):
        return {"duration": 5401.0}

    async def whisper(_video_id):
        pytest.fail("Unknown duration must not bypass the 90 minute audio limit")

    monkeypatch.setattr(candidates, "fetch_media_info", metadata)
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    assert (
        await candidates._whisper_workaround(candidates.Candidate(A, "A", None)) is None
    )


@pytest.mark.asyncio
async def test_empty_transcript_is_a_typed_failure():
    with pytest.raises(candidates.CandidateSearchError, match="Transcript is empty"):
        await candidates.find_source_video("  ...  ")
