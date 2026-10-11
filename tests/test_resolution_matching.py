"""Offline regressions for the supplied source matcher and timed localization."""

from types import SimpleNamespace

import httpx
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


@pytest.mark.asyncio
@pytest.mark.parametrize("source_rank", range(7, 13))
async def test_expanded_caption_budget_recovers_later_source(monkeypatch, source_rank):
    from unittest.mock import AsyncMock
    ids = [f"R{i:010d}" for i in range(12)]
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={
        "query": "speech", "title_query": "", "unique_phrase": "speech", "speaker_and_event": ""}))
    monkeypatch.setattr(candidates, "_search", AsyncMock(return_value=[
        candidates.Candidate(video_id, "Full recording", 900) for video_id in ids]))
    async def captions(candidate):
        return transcript(PASSAGE if candidate.video_id == ids[source_rank-1] else "Unrelated engineering equipment calibration.")
    monkeypatch.setattr(candidates, "_candidate_captions", captions)
    audio = AsyncMock(side_effect=AssertionError("Caption leads must not require audio"))
    monkeypatch.setattr(candidates, "_whisper_workaround", audio)
    result = await candidates.find_source_video(PASSAGE)
    assert result["video_id"] == ids[source_rank-1] and result["status"] == "success"
    assert len(result["leads"]) == 12
    audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_caption_failures_have_six_audio_attempts_after_all_captions(monkeypatch):
    from unittest.mock import AsyncMock
    ids = [f"R{i:010d}" for i in range(12)]
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=[
        candidates.Candidate(video_id, "Recording", 900) for video_id in ids]))
    captioned = []
    async def captions(candidate):
        captioned.append(candidate.video_id)
    audio = []
    async def whisper(candidate):
        assert len(captioned) == 12
        audio.append(candidate.video_id)
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(candidates, "_candidate_captions", captions)
    monkeypatch.setattr(candidates, "_whisper_workaround", whisper)
    result = await candidates.find_source_video(PASSAGE)
    assert result["status"] == "not_found" and audio == ids[:6]
    assert len(result["leads"]) == 12


@pytest.mark.asyncio
async def test_rank_five_missing_caption_source_keeps_prior_audio_recovery(monkeypatch):
    from unittest.mock import AsyncMock
    ids = [f"R{i:010d}" for i in range(8)]
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=[
        candidates.Candidate(video_id, "Recording", 300) for video_id in ids]))
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value=None))
    audio = []
    async def whisper(candidate):
        audio.append(candidate.video_id)
        return transcript(PASSAGE) if candidate.video_id == ids[4] else None
    monkeypatch.setattr(candidates, "_whisper_workaround", whisper)
    result = await candidates.find_source_video(PASSAGE)
    assert result["status"] == "success" and result["video_id"] == ids[4]
    assert audio == ids[:6]


@pytest.mark.asyncio
@pytest.mark.parametrize("durations,expected", [([7200, 7200, *([300] * 6)], list(range(2, 8))),
                                                ([*([None] * 6), 300, 300], list(range(6)))])
async def test_audio_budget_skips_known_ineligible_but_counts_unknown_probes(monkeypatch, durations, expected):
    from unittest.mock import AsyncMock
    ids = [f"R{i:010d}" for i in range(8)]
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=[
        candidates.Candidate(video_id, "Recording", duration) for video_id, duration in zip(ids, durations)]))
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value=None))
    audio = AsyncMock(return_value=None)
    monkeypatch.setattr(candidates, "_whisper_workaround", audio)
    events = []
    monkeypatch.setattr(candidates, "record", lambda stage, status, data, **kwargs: events.append(data))
    result = await candidates.find_source_video(PASSAGE)
    assert [call.args[0].video_id for call in audio.await_args_list] == [ids[i] for i in expected]
    assert len(result["leads"]) == 8 and result["status"] == "not_found"
    if durations[0] is not None:
        assert sum(data.get("reason") == "audio_duration_outside_budget" for data in events) == 2
        assert not any(data.get("reason") == "audio_fallback_budget_exhausted" for data in events)
    else:
        assert sum(data.get("reason") == "audio_fallback_budget_exhausted" for data in events) == 2


@pytest.mark.asyncio
async def test_secondary_observed_archive_phrase_recovers_one_asr_anchor_error(tmp_path):
    from datetime import UTC, datetime

    from backend.services.storage.tiger import TigerStore

    text = candidates.normalize_text(PASSAGE + " The committee subsequently documented independent observations supporting continued scientific exploration.")
    words = text.split()[:70]
    # The injected token is absent everywhere in the recording, making the first
    # conjunctive archive query fail without relying on a lexical score threshold.
    anchor = [*words[:7], "nonexistentaudiotoken"]
    submitted = " ".join([*anchor, *words[8:]])
    store = TigerStore(f"sqlite:///{tmp_path / 'archive.db'}")
    store.ensure_schema()
    post = {"post_id": "source", "post_url": "https://youtu.be/AAAAAAAAAAA", "platform": "youtube",
            "author": {}, "engagement": {}, "media": {"media_id": "source", "local_path": "/missing.mp4", "mime_type": "video/mp4"},
            "retrieval": {"retrieved_at": datetime.now(UTC).isoformat(), "provider": "fixture"}}
    features = {"media_id": "source", "duration_seconds": 60,
                "transcript": {"status": "complete", "sentences": [
                    {"sentence_id": "s1", "text": " ".join(words), "start_seconds": 0, "end_seconds": 60}]}}
    try:
        store.cache_source_features(post, features, "a" * 64, "b" * 64)
        assert await candidates._archive_candidates(store, " ".join(anchor), set()) == []
        recovered = await candidates._archive_candidates(store, " ".join(anchor), set(), transcript=submitted)
        assert [candidate.video_id for candidate in recovered] == ["AAAAAAAAAAA"]
        assert await candidates._archive_candidates(store, " ".join(anchor), {"AAAAAAAAAAA"}, transcript=submitted) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_archive_secondary_query_requires_distinct_observed_substantive_words():
    calls = []
    store = SimpleNamespace(find_transcript_posts=lambda queries, *args: calls.append(queries) or [])
    for transcript in ["", "one two three four five six", "one two three four five six " + "the " * 20]:
        await candidates._archive_candidates(store, "one two three four five six", set(), transcript=transcript)
    assert calls == [["one two three four five six"]] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_fails", [False, True])
async def test_secondary_observed_web_quote_recovers_corrupted_anchor_with_bounded_queries(monkeypatch, primary_fails):
    from unittest.mock import AsyncMock
    anchor = "the observatory measured hydrogen emissions from distant nonexistentaudiotoken"
    submitted = anchor + " The expedition returned with unprecedented photographic evidence supporting scientific exploration."
    secondary = candidates._secondary_phrase(submitted, anchor)
    meta = {"query": "wrong speaker hypothesis", "title_query": "wrong celebrity title",
            "unique_phrase": anchor, "speaker_and_event": "wrong speaker"}
    monkeypatch.setattr(candidates, "extract_search_metadata", AsyncMock(return_value=meta))
    searched = []
    async def search(query, limit):
        searched.append(query)
        if query != candidates.build_query("", secondary):
            if primary_fails:
                raise candidates.CandidateSearchError("search_failed", "Offline simulated failure")
            return []
        return [candidates.Candidate(SELF, "self", 900), candidates.Candidate(A, "short", 10),
                candidates.Candidate(B, "Source", 900), candidates.Candidate(B, "Duplicate", 900)]
    monkeypatch.setattr(candidates, "_search", search)
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value=transcript(submitted)))
    result = await candidates.find_source_video(submitted, exclude_ids=[SELF], discovery_hints=["OCR: NEWS NETWORK", "Scene: podium audience"])
    assert result["status"] == "success" and result["video_id"] == B
    assert [lead["video_id"] for lead in result["leads"]] == [B]
    assert len(searched) == len(set(searched)) == candidates.MAX_SEARCH_QUERIES
    assert result["search_metadata"]["secondary_phrase"] == secondary


def test_visual_search_clues_are_bounded_and_do_not_replace_the_speech_anchor():
    phrase = "the observatory measured hydrogen emissions from distant galaxies"
    queries = candidates._visual_queries(
        [None, "https://untrusted.example/source", "OCR: PBS NEWS HOUR", "OCR: STRONGER ECONOMY"], phrase
    )
    assert queries == [
        f'pbs news hour "{phrase}"', f'stronger economy "{phrase}"'
    ]
    assert candidates._visual_queries("PBS NEWS HOUR", phrase) == []
    assert candidates._visual_queries(["x" * 161, "one"], phrase) == []
    assert len(candidates._visual_queries(["one two", "three four", "five six"], phrase)) == 2


@pytest.mark.asyncio
async def test_archive_excludes_the_submitted_identity_before_its_query_limit():
    def lookup(queries, limit, excluded_identities):
        assert queries == [PASSAGE]
        assert limit == candidates.MAX_CANDIDATES
        assert excluded_identities == [f"youtube:{SELF}"]
        return [({"post_url": f"https://youtu.be/{A}", "title": "Full recording"}, [])]

    result = await candidates._archive_candidates(
        SimpleNamespace(find_transcript_posts=lookup), PASSAGE, {SELF}
    )
    assert [item.video_id for item in result] == [A]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_speech", [True, False])
async def test_visual_clue_can_discover_a_source_but_cannot_verify_unrelated_speech(
    monkeypatch, matching_speech
):
    phrase = " ".join(candidates.normalize_text(PASSAGE).split()[:8])
    queried = []

    async def search(query, limit):
        queried.append(query)
        return [candidates.Candidate(C, "Possible fuller recording", 900)] if query.startswith("pbs news hour ") else []

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(candidates, "_search", search)
    monkeypatch.setattr(candidates, "_fallback_phrase", lambda *args: phrase)
    monkeypatch.setattr(
        candidates, "_fetch_youtube_captions_sync",
        lambda *args, **kwargs: transcript(PASSAGE if matching_speech else "A garden contains flowers beside a pond."),
    )
    result = await candidates.find_source_video(
        PASSAGE, discovery_hints=["PBS NEWS HOUR", "STRONGER ECONOMY", "third unused clue"]
    )
    assert candidates.build_query("", phrase) in queried
    assert len(queried) <= 5
    assert result["video_id"] == C
    assert result["status"] == ("success" if matching_speech else "low_confidence")
    assert any("Visual search clues" in warning for warning in result["warnings"])


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
                candidates.Candidate("SHORTSHORT1", "Short", 29),
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
    assert [item.video_id for item in result] == [A, B, C, D]


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
async def test_empty_searches_loosen_the_query_and_deduplicate_leads(
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

    def captions(video_id, **_kwargs):
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
    assert sorted(checked) == [A, B, C]
    assert result["status"] == "success"
    assert result["video_id"] == B
    assert [item["video_id"] for item in result["candidates"]] == [B, C]
    assert [item["video_id"] for item in result["leads"]] == [B, C, A]
    assert all("transcript_result" not in lead for lead in result["leads"])
    assert result["warnings"]
    assert result["candidates"][0]["transcript_result"]["segments"][0]["start"] == 120.0


@pytest.mark.asyncio
async def test_candidate_without_captions_uses_timed_whisper_result(monkeypatch):
    def captions(_video_id, **_kwargs):
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
async def test_discovery_duration_is_passed_to_hosted_caption_timing(monkeypatch):
    observed = []

    def captions(video_id, *, known_duration):
        observed.append((video_id, known_duration))
        return transcript(PASSAGE)

    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    result = await candidates._score_candidate(
        candidates.Candidate(A, "Full recording", 900.0),
        candidates.normalize_text(PASSAGE),
    )
    assert observed == [(A, 900.0)]
    assert result["match_score"] == 100.0


@pytest.mark.asyncio
async def test_whisper_fallback_skips_recordings_longer_than_ninety_minutes(
    monkeypatch,
):
    def captions(_video_id, **_kwargs):
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


@pytest.fixture
def serpapi_search(monkeypatch):
    for name in (
        "YOUTUBE_API_KEY",
        "SEARCH_PROVIDER",
        "SEARCH_PROVIDER_API_KEY",
        "SEARCH_FALLBACK_PROVIDER",
        "SEARCH_FALLBACK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    real_client = httpx.Client
    calls = []

    def configure(payload, status=200, headers=None):
        def handle(request):
            calls.append(request)
            return httpx.Response(status, json=payload, headers=headers)

        def client(**kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            assert kwargs["timeout"] == 12.0
            assert kwargs["verify"] is not False
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(candidates.httpx, "Client", client)
        return calls

    return configure


def test_serpapi_youtube_request_parses_only_supported_recordings(serpapi_search):
    calls = serpapi_search(
        {
            "video_results": [
                {
                    "link": f"https://www.youtube.com/watch?v={A}",
                    "title": "Full speech",
                    "length": "32:00",
                },
                {
                    "link": f"https://youtu.be/{B}",
                    "video_id": B,
                    "title": "Long interview",
                    "length": "1:02:03",
                },
                {"video_id": C, "title": "Live recording"},
                {
                    "link": f"https://www.youtube.com/watch?v={D}",
                    "title": "Short",
                    "length": "2:59",
                },
                {"video_id": A, "link": f"https://example.com/watch?v={A}"},
                {"video_id": A, "link": f"https://youtube.com/watch?v={B}"},
                {"link": f"https://youtube.com@evil.example/watch?v={A}"},
                {"link": f"https://www.youtube.com:123/watch?v={A}"},
                {"video_id": "invalid"},
                None,
            ]
        }
    )
    found = candidates._search_serpapi_sync("exact transcript query", "offline-key", 12)
    assert [(item.video_id, item.duration) for item in found] == [
        (A, 1920.0),
        (B, 3723.0),
        (C, None),
        (D, 179.0),
    ]
    assert found[1].url == f"https://www.youtube.com/watch?v={B}"
    assert len(calls) == 1
    assert str(calls[0].url).startswith("https://serpapi.com/search.json?")
    assert dict(calls[0].url.params) == {
        "engine": "youtube",
        "search_query": "exact transcript query",
        "api_key": "offline-key",
    }


@pytest.mark.parametrize(
    "duration", [None, "LIVE", "1:99", "1:60:00", "-3:00", "３:００", 900, True]
)
def test_serpapi_unknown_or_invalid_length_does_not_invent_duration(duration):
    assert candidates._serpapi_duration(duration) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
@pytest.mark.parametrize("data_api", [False, True])
async def test_serpapi_recovers_empty_or_failed_direct_search(
    monkeypatch, serpapi_search, provider_failure, data_api
):
    calls = serpapi_search(
        {"video_results": [{"video_id": A, "title": "Original", "length": "30:00"}]}
    )
    monkeypatch.setenv("SEARCH_FALLBACK_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_FALLBACK_API_KEY", "offline-key")
    if data_api:
        monkeypatch.setenv("YOUTUBE_API_KEY", "offline-youtube-key")

    def direct(*_args):
        if provider_failure:
            raise RuntimeError("Offline direct provider failure")
        return []

    monkeypatch.setattr(candidates, "_search_ytdlp_sync", direct)
    monkeypatch.setattr(candidates, "_search_data_api_sync", direct)
    found = await candidates._search("speech query", 12)
    assert [item.video_id for item in found] == [A]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_successful_direct_search_does_not_spend_a_serpapi_request(
    monkeypatch, serpapi_search
):
    calls = serpapi_search({"video_results": []})
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "offline-key")
    monkeypatch.setattr(
        candidates, "_search_ytdlp_sync", lambda *_: [candidates.Candidate(A, "A", 900)]
    )
    assert (await candidates._search("query", 12))[0].video_id == A
    assert calls == []


@pytest.mark.asyncio
async def test_serpapi_fallback_preserves_interleaving_self_and_duration_filters(
    monkeypatch, serpapi_search
):
    calls = serpapi_search(
        {
            "video_results": [
                {"video_id": SELF, "length": "30:00"},
                {"video_id": D, "length": "0:29"},
                {"video_id": A, "length": "3:00"},
                {"video_id": B, "length": "20:00"},
                {"video_id": C, "length": "30:00"},
            ]
        }
    )
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "offline-key")
    monkeypatch.setattr(candidates, "_search_ytdlp_sync", lambda *_: [])
    found = await candidates.search_candidates(
        {
            "query": '"speaker event" "actual verbatim transcript phrase"',
            "title_query": "independent original title",
            "speaker_and_event": "speaker event",
            "unique_phrase": "actual verbatim transcript phrase",
        },
        [SELF],
    )
    assert [item.video_id for item in found] == [A, B, C]
    assert len(calls) == 2
    assert {request.url.params["search_query"] for request in calls} == {
        '"speaker event" "actual verbatim transcript phrase"',
        "independent original title",
    }


@pytest.mark.asyncio
async def test_failed_serpapi_search_has_redacted_public_error(
    monkeypatch, serpapi_search
):
    calls = serpapi_search(
        {"error": "diagnostic containing offline-private-key"}, status=403
    )
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "offline-private-key")

    def direct(*_):
        raise RuntimeError("Offline direct failure")

    monkeypatch.setattr(candidates, "_search_ytdlp_sync", direct)
    with pytest.raises(candidates.CandidateSearchError) as failure:
        await candidates._search("query", 12)
    assert "private" not in str(failure.value)
    assert "serpapi" not in str(failure.value)
    assert failure.value.__cause__ is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "settings, expected",
    [
        (
            {"SEARCH_PROVIDER": "serpapi", "SEARCH_PROVIDER_API_KEY": "primary"},
            "primary",
        ),
        (
            {
                "SEARCH_FALLBACK_PROVIDER": "serpapi",
                "SEARCH_FALLBACK_API_KEY": "fallback",
            },
            "fallback",
        ),
        (
            {
                "SEARCH_PROVIDER": "serpapi",
                "SEARCH_PROVIDER_API_KEY": "primary",
                "SEARCH_FALLBACK_PROVIDER": "serpapi",
                "SEARCH_FALLBACK_API_KEY": "fallback",
            },
            "fallback",
        ),
        ({"SEARCH_PROVIDER": "exa", "SEARCH_PROVIDER_API_KEY": "other-provider"}, None),
        (
            {
                "SEARCH_FALLBACK_PROVIDER": "serpapi",
                "SEARCH_PROVIDER": "exa",
                "SEARCH_PROVIDER_API_KEY": "other-provider",
            },
            None,
        ),
    ],
)
def test_only_explicit_serpapi_provider_key_pair_is_used(
    monkeypatch, serpapi_search, settings, expected
):
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert candidates._serpapi_key() == expected


@pytest.mark.asyncio
async def test_optional_serpapi_failure_does_not_erase_successful_empty_search(
    monkeypatch, serpapi_search
):
    calls = serpapi_search({"error": "Offline quota failure"})
    monkeypatch.setenv("SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("SEARCH_PROVIDER_API_KEY", "offline-key")
    monkeypatch.setattr(candidates, "_search_ytdlp_sync", lambda *_: [])
    assert await candidates._search("query", 12) == []
    assert len(calls) == 1


def test_serpapi_response_limit_is_enforced(serpapi_search):
    serpapi_search({"video_results": []}, headers={"content-length": "999999999"})
    with pytest.raises(ValueError, match="byte limit"):
        candidates._search_serpapi_sync("query", "offline-key", 12)


@pytest.mark.asyncio
async def test_source_leads_show_highest_scored_candidate_before_unavailable_leads(
    monkeypatch,
):
    async def metadata(*_):
        return {}

    async def search(*_):
        return [candidates.Candidate(video_id, video_id, 900) for video_id in (A, B, C)]

    def captions(video_id, **_kwargs):
        if video_id == C:
            raise RuntimeError("Offline missing captions")
        return transcript(
            PASSAGE
            if video_id == B
            else "An unrelated cooking demonstration describes preparing vegetables and baking bread."
        )

    async def whisper(_video_id):
        raise RuntimeError("Offline audio unavailable")

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "search_candidates", search)
    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    result = await candidates.find_source_video(PASSAGE)
    assert result["video_id"] == B
    assert [lead["video_id"] for lead in result["leads"]] == [B, A, C]
    assert all("transcript_result" not in lead for lead in result["leads"])


@pytest.mark.asyncio
async def test_caption_prefetch_is_bounded_and_keeps_ties_in_search_order(monkeypatch):
    import asyncio

    active, peak, finished = 0, 0, []

    async def metadata(*args):
        return {}

    async def search(*args):
        return [candidates.Candidate(identity, identity, 900) for identity in (A, B, C)]

    async def captions(candidate):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02 if candidate.video_id == A else 0.005)
        finished.append(candidate.video_id)
        active -= 1
        return transcript(PASSAGE)

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "search_candidates", search)
    monkeypatch.setattr(candidates, "_candidate_captions", captions)
    result = await candidates.find_source_video(PASSAGE)
    assert peak == 2 and finished[0] == B
    assert [row["video_id"] for row in result["candidates"]] == [A, B, C]


@pytest.mark.asyncio
async def test_failed_prefetch_is_not_retried_and_whisper_fallback_stays_serial(
    monkeypatch,
):
    import asyncio
    from unittest.mock import Mock

    calls, active, peak = [], 0, 0

    async def metadata(*args):
        return {}

    async def search(*args):
        return [candidates.Candidate(identity, identity, 900) for identity in (A, B, C)]

    def captions(video_id, **kwargs):
        calls.append(video_id)
        if video_id == B:
            return transcript(PASSAGE)
        raise RuntimeError("Offline caption failure")

    async def whisper(video_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        if video_id == A:
            raise RuntimeError("Offline audio failure")
        return transcript(PASSAGE)

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "search_candidates", search)
    monkeypatch.setattr(
        candidates, "_fetch_youtube_captions_sync", Mock(side_effect=captions)
    )
    monkeypatch.setattr(candidates, "transcribe_youtube_audio", whisper)
    result = await candidates.find_source_video(PASSAGE)
    assert sorted(calls) == [A, B, C] and peak == 1
    assert [row["video_id"] for row in result["candidates"]] == [B, C]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_matches", [True, False])
async def test_independent_transcript_anchor_recovers_wrong_metadata_without_verifying_unrelated_hits(
    monkeypatch, source_matches
):
    phrase = " ".join(candidates.normalize_text(PASSAGE).split()[:8])
    phrase_query = candidates.build_query("", phrase)
    queried = []

    async def metadata(*args):
        return {
            "speaker_and_event": "Incorrect person and event",
            "unique_phrase": phrase,
            "query": candidates.build_query("Incorrect person and event", phrase),
            "title_query": "misleading repost title",
        }

    async def search(query, limit):
        queried.append(query)
        if query == phrase_query:
            return [candidates.Candidate(C, "Original source recording", 900)]
        return [
            candidates.Candidate(
                A if "Incorrect" in query else B, "Unrelated event", 900
            )
        ]

    def captions(video_id, **kwargs):
        text = (
            PASSAGE
            if video_id == C and source_matches
            else "Small kittens play in a garden while their owners make lunch inside the house."
        )
        return transcript(text)

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "_search", search)
    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    result = await candidates.find_source_video(PASSAGE)
    assert phrase_query in queried and len(queried) <= candidates.MAX_SEARCH_QUERIES
    assert len(result["candidates"]) <= candidates.MAX_CANDIDATES
    if source_matches:
        assert result["status"] == "success" and result["video_id"] == C
    else:
        assert result["status"] == "low_confidence"
        assert result["match_score"] < candidates.MATCH_THRESHOLD


@pytest.mark.asyncio
async def test_ninety_second_source_is_discovered_but_tiny_duplicate_is_filtered(
    monkeypatch,
):
    async def search(*args):
        return [
            candidates.Candidate(A, "Tiny repost", 29),
            candidates.Candidate(B, "Original recording", 90),
        ]

    monkeypatch.setattr(candidates, "_search", search)
    result = await candidates.search_candidates(
        {
            "query": "source",
            "title_query": "",
            "speaker_and_event": "",
            "unique_phrase": "",
        }
    )
    assert [row.video_id for row in result] == [B]
    assert result[0].duration == 90


@pytest.mark.asyncio
async def test_rank_five_original_is_scored_with_expanded_candidate_bound(monkeypatch):
    checked = []
    identities = [f"{index:011d}" for index in range(1, 8)]

    async def metadata(*args):
        return {
            "query": "source",
            "title_query": "",
            "speaker_and_event": "",
            "unique_phrase": "",
        }

    async def search(*args):
        return [candidates.Candidate(identity, identity, 90) for identity in identities]

    def captions(video_id, **kwargs):
        checked.append(video_id)
        text = (
            PASSAGE
            if video_id == identities[4]
            else "Small kittens play beside the kitchen while their owners prepare dinner for visiting relatives."
        )
        return {**transcript(text, start=20, end=35), "duration": 90.0}

    monkeypatch.setattr(candidates, "extract_search_metadata", metadata)
    monkeypatch.setattr(candidates, "_search", search)
    monkeypatch.setattr(candidates, "_fetch_youtube_captions_sync", captions)
    result = await candidates.find_source_video(PASSAGE)
    assert result["status"] == "success" and result["video_id"] == identities[4]
    assert sorted(checked) == identities
    assert len(result["candidates"]) == 7 and len(result["leads"]) == 7


@pytest.mark.parametrize("replacement", ["helium", "oxygen", "carbon"])
def test_near_repeated_passage_does_not_choose_arbitrary_timestamp(replacement):
    clip, source = pair()
    source.transcript.sentences.append(
        type(source.transcript.sentences[0]).model_validate(
            sentence(
                "source_near_repeat", PASSAGE.replace("hydrogen", replacement), 400, 415
            )
        )
    )
    result = build_alignment(clip, source, 100.0)
    assert result.status == "no_verified_match"
    assert not result.matches
    assert any("ambiguous" in note for note in result.limitations)


def test_distinct_other_passage_keeps_unique_timestamp():
    clip, source = pair()
    source.transcript.sentences.append(
        type(source.transcript.sentences[0]).model_validate(
            sentence(
                "different_passage",
                "The observatory announced a new public exhibition about the history of astronomy and invited local schools to attend next month.",
                400,
                415,
            )
        )
    )
    result = build_alignment(clip, source, 100.0)
    assert result.status == "verified"
    assert result.matches[0].source_start_seconds == 125.5


@pytest.mark.asyncio
async def test_archive_lead_survives_blocked_web_and_reaches_timed_alignment(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    store = SimpleNamespace(
        find_transcript_posts=lambda queries, limit, excluded=(): [
            (
                {
                    "post_url": f"https://www.youtube.com/watch?v={A}",
                    "title": "Full speech",
                },
                [PASSAGE],
            )
        ]
    )
    monkeypatch.setattr(
        candidates,
        "extract_search_metadata",
        AsyncMock(
            return_value={
                "query": "speech",
                "title_query": "",
                "speaker_and_event": "",
                "unique_phrase": PASSAGE[:100],
            }
        ),
    )
    monkeypatch.setattr(
        candidates,
        "search_candidates",
        AsyncMock(
            side_effect=candidates.CandidateSearchError("search_failed", "blocked")
        ),
    )
    monkeypatch.setattr(
        candidates,
        "_fetch_youtube_captions_sync",
        lambda *args, **kwargs: transcript(PASSAGE),
    )
    found = await candidates.find_source_video(PASSAGE, store=store)
    assert found["status"] == "success" and found["video_id"] == A
    clip, _ = pair()
    timed = found["candidates"][0]["transcript_result"]
    source = features(
        "archive_recording",
        [
            sentence(f"source_{i}", segment["text"], segment["start"], segment["end"])
            for i, segment in enumerate(timed["segments"])
        ],
    )
    assert build_alignment(clip, source, found["match_score"]).status == "verified"


@pytest.mark.asyncio
async def test_archive_untimed_text_does_not_become_recording_evidence(monkeypatch):
    from unittest.mock import AsyncMock

    store = SimpleNamespace(
        find_transcript_posts=lambda queries, limit, excluded=(): [
            (
                {
                    "post_url": f"https://www.youtube.com/watch?v={A}",
                    "title": PASSAGE,
                    "transcript": PASSAGE,
                },
                [],
            )
        ]
    )
    monkeypatch.setattr(
        candidates,
        "extract_search_metadata",
        AsyncMock(
            return_value={
                "query": "speech",
                "title_query": "",
                "speaker_and_event": "",
                "unique_phrase": "",
            }
        ),
    )
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        candidates, "_fetch_youtube_captions_sync", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(candidates, "_whisper_workaround", AsyncMock(return_value=None))
    found = await candidates.find_source_video(PASSAGE, store=store)
    assert found["status"] == "not_found" and not found["candidates"]


@pytest.mark.asyncio
async def test_archive_rejects_malformed_urls_deduplicates_and_excludes_self():
    urls = [
        f"https://www.youtube.com/watch?v={A}",
        f"https://youtu.be/{A}",
        f"https://evil.example/watch?v={B}",
        f"https://user@youtube.com/watch?v={B}",
        f"https://youtube.com:99/watch?v={B}",
        f"https://youtube.com/watch?v={SELF}",
    ]
    store = SimpleNamespace(
        find_transcript_posts=lambda queries, limit, excluded=(): [
            ({"post_url": url}, []) for url in urls
        ]
    )
    result = await candidates._archive_candidates(store, PASSAGE, {SELF})
    assert [candidate.video_id for candidate in result] == [A]


@pytest.mark.asyncio
async def test_archive_failure_preserves_web_and_merge_reserves_web_budget(monkeypatch):
    from unittest.mock import AsyncMock

    meta = {
        "query": "speech",
        "title_query": "",
        "speaker_and_event": "",
        "unique_phrase": "",
    }
    web = [candidates.Candidate(f"{i:011d}", "Web speech", 90) for i in range(1, 7)]
    monkeypatch.setattr(
        candidates, "extract_search_metadata", AsyncMock(return_value=meta)
    )
    monkeypatch.setattr(candidates, "search_candidates", AsyncMock(return_value=web))
    monkeypatch.setattr(candidates, "_candidate_captions", AsyncMock(return_value=None))
    monkeypatch.setattr(candidates, "_whisper_workaround", AsyncMock(return_value=None))
    broken = SimpleNamespace(
        find_transcript_posts=lambda *args: (_ for _ in ()).throw(
            RuntimeError("offline")
        )
    )
    result = await candidates.find_source_video(PASSAGE, store=broken)
    assert [lead["video_id"] for lead in result["leads"]] == [
        item.video_id for item in web
    ]
    local = SimpleNamespace(
        find_transcript_posts=lambda *args: [
            ({"post_url": f"https://youtu.be/{A}"}, []),
            ({"post_url": f"https://youtu.be/{B}"}, []),
        ]
    )
    result = await candidates.find_source_video(PASSAGE, store=local, max_candidates=6)
    assert [lead["video_id"] for lead in result["leads"]] == [
        item.video_id for item in web[:4]
    ] + [A, B]
    assert len(result["leads"]) == 6


@pytest.mark.asyncio
async def test_nonempty_irrelevant_quotes_do_not_starve_relaxed_observed_query(monkeypatch):
    primary = "god works all things for good"
    secondary = "our administration proposed comprehensive infrastructure investments supporting communities"
    queries = []

    async def search(query, _limit):
        queries.append(query)
        if query == secondary:
            return [candidates.Candidate(SELF, "Self", 900),
                    candidates.Candidate(A, "Short excerpt", 10),
                    candidates.Candidate(B, "Actual source", 900),
                    candidates.Candidate(B, "Duplicate source", 900)]
        return [candidates.Candidate(C, "Unrelated sermon", 900)]

    monkeypatch.setattr(candidates, "_search", search)
    result = await candidates.search_candidates({
        "speaker_and_event": "Unverified speaker",
        "query": '"Unverified speaker" "god works all things for good"',
        "title_query": "Unverified title",
        "unique_phrase": primary,
        "secondary_phrase": secondary,
        "visual_queries": ["first visual clue", "second visual clue"],
    }, [SELF])
    assert secondary in queries
    assert primary not in queries
    assert len(queries) == len(set(queries)) == candidates.MAX_SEARCH_QUERIES
    assert [item.video_id for item in result] == [C, B]
    assert len(result) <= candidates.MAX_CANDIDATES


@pytest.mark.asyncio
@pytest.mark.parametrize("segments", [None, [], [{"text": PASSAGE, "start": 10, "end": 5}], [{"text": PASSAGE, "start": 0, "end": float("inf")}], [{"text": PASSAGE, "start": True, "end": 10}]])
async def test_untimed_caption_text_uses_existing_whisper_fallback(monkeypatch, segments):
    from unittest.mock import AsyncMock
    caption = {"transcript": PASSAGE, "segments": segments}
    fallback = AsyncMock(return_value={**transcript(PASSAGE), "source": "groq_whisper"})
    monkeypatch.setattr(candidates, "_whisper_workaround", fallback)
    candidate = candidates.Candidate(A, "Recording", 900)
    assert await candidates._score_candidate(candidate, candidates.normalize_text(PASSAGE), caption_result=caption, allow_whisper=False) is None
    fallback.assert_not_awaited()
    result = await candidates._score_candidate(candidate, candidates.normalize_text(PASSAGE), caption_result=caption)
    fallback.assert_awaited_once_with(candidate)
    assert result["match_score"] == 100
    assert result["transcript_result"]["source"] == "groq_whisper"


@pytest.mark.asyncio
async def test_valid_timed_segments_recover_missing_transcript_string_without_provider(monkeypatch):
    from unittest.mock import AsyncMock
    observed = {**transcript(PASSAGE), "transcript": ""}
    fallback = AsyncMock(side_effect=AssertionError("Usable timed speech needs no audio call"))
    monkeypatch.setattr(candidates, "_whisper_workaround", fallback)
    result = await candidates._score_candidate(candidates.Candidate(A, "Recording", 900), candidates.normalize_text(PASSAGE), caption_result=observed)
    assert result["match_score"] == 100
    assert result["transcript_result"] is observed
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_untimed_whisper_text_is_not_promoted_to_candidate(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(candidates, "_whisper_workaround", AsyncMock(return_value={"transcript": PASSAGE, "segments": []}))
    result = await candidates._score_candidate(candidates.Candidate(A, "Recording", 900), candidates.normalize_text(PASSAGE), caption_result=None)
    assert result is None


@pytest.mark.asyncio
async def test_untimed_matching_text_cannot_inflate_observed_segment_score():
    observed = {"transcript": PASSAGE, "segments": [
        {"text": "A completely unrelated garden and its flowers.", "start": 1, "end": 5},
        {"text": PASSAGE, "start": 5},  # Hosted final cue has no observed end.
    ]}
    result = await candidates._score_candidate(candidates.Candidate(A, "Recording", 900), candidates.normalize_text(PASSAGE), caption_result=observed, allow_whisper=False)
    assert result["match_score"] < candidates.MATCH_THRESHOLD
