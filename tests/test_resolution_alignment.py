"""Source localization must preserve potentially meaning-changing transcript differences."""

import pytest
from rapidfuzz import fuzz

from backend.services.resolution.alignment import build_alignment
from backend.services.resolution.candidates import normalize_text
from backend.services.shared.types import MediaFeatures

PASSAGE = (
    "The committee reviewed emergency funding proposals before announcing that our "
    "administration will not authorize 30 additional surveillance systems without "
    "independent legislative oversight and public consultation."
)


def features(media_id, text):
    return MediaFeatures.model_validate(
        {
            "media_id": media_id,
            "duration_seconds": 20,
            "transcript": {
                "status": "complete",
                "processed_ranges": [{"start_seconds": 0, "end_seconds": 20}],
                "sentences": [
                    {
                        "sentence_id": media_id + "_s1",
                        "text": text,
                        "start_seconds": 0,
                        "end_seconds": 20,
                    }
                ],
            },
        }
    )


@pytest.mark.parametrize(
    "text, flag",
    [
        (PASSAGE.replace("will not", "will"), "negation"),
        (PASSAGE.replace("30", "300"), "numeric"),
    ],
)
def test_meaning_sensitive_differences_keep_source_but_downgrade_confidence(text, flag):
    clip, source = features("clip", text), features("source", PASSAGE)
    score = fuzz.partial_ratio(normalize_text(text), normalize_text(PASSAGE))
    result = build_alignment(clip, source, score)
    assert result.status == "verified"
    assert result.match_quality.confidence.level == "moderate"
    assert any(flag + " wording differs" in item for item in result.limitations)
    assert result.matches[0].source_start_seconds == 0
    assert result.matches[0].source_end_seconds == 20
    assert clip.transcript.sentences[0].text == text
    assert source.transcript.sentences[0].text == PASSAGE


def test_identical_negation_and_numbers_do_not_reduce_confidence():
    result = build_alignment(
        features("clip", PASSAGE), features("source", PASSAGE), 100
    )
    assert result.status == "verified"
    assert result.match_quality.confidence.level == "high"
    assert not any("wording differs" in item for item in result.limitations)


@pytest.mark.parametrize(
    "negative, positive",
    [
        ("don't authorize", "authorize"),
        ("isn't authorized", "is authorized"),
        ("cannot authorize", "can authorize"),
    ],
)
def test_contracted_negations_are_flagged_without_rewriting(negative, positive):
    source_text = PASSAGE.replace("will not authorize", negative)
    clip_text = PASSAGE.replace("will not authorize", positive)
    result = build_alignment(
        features("clip", clip_text), features("source", source_text), 100
    )
    assert result.status == "verified"
    assert result.match_quality.confidence.level == "moderate"
    assert any("negation wording differs" in item for item in result.limitations)
    assert any(
        "submitted '" in item and "source '" in item
        for item in result.match_quality.confidence.basis
    )


def test_already_moderate_similarity_is_not_promoted_by_difference_flag():
    result = build_alignment(
        features("clip", PASSAGE.replace("30", "300")), features("source", PASSAGE), 80
    )
    assert result.status == "verified"
    assert result.match_quality.confidence.level == "moderate"


@pytest.mark.parametrize("left, right", [("30", "30.0"), ("30000", "30,000")])
def test_equivalent_numeric_formatting_does_not_flag_difference(left, right):
    result = build_alignment(
        features("clip", PASSAGE.replace("30", left)),
        features("source", PASSAGE.replace("30", right)),
        100,
    )
    assert result.status == "verified"
    assert not any("numeric wording differs" in item for item in result.limitations)


def test_decimal_value_is_preserved_before_punctuation_normalization():
    result = build_alignment(
        features("clip", PASSAGE.replace("30", "3.0")), features("source", PASSAGE), 100
    )
    assert result.status == "verified"
    assert result.match_quality.confidence.level == "moderate"
    assert any("numeric wording differs" in item for item in result.limitations)


@pytest.mark.parametrize(
    "left, right", [("don't", "do not"), ("cannot", "can not"), ("isn't", "is not")]
)
def test_equivalent_negation_forms_do_not_flag_difference(left, right):
    result = build_alignment(
        features("clip", PASSAGE.replace("will not", left)),
        features("source", PASSAGE.replace("will not", right)),
        100,
    )
    assert result.status == "verified"
    assert not any("negation wording differs" in item for item in result.limitations)


def test_partially_matched_number_abstains_from_numeric_comparison():
    from backend.services.resolution.alignment import _matched_numbers

    text = "300 additional surveillance systems"
    assert _matched_numbers(features("source", text), 1, len(text)) is None
    assert _matched_numbers(features("source", text), 0, 2) is None


def test_numeric_token_ownership_across_sentences():
    from decimal import Decimal

    from backend.services.resolution.alignment import _matched_numbers

    source = features("source", "An introduction.")
    row = source.transcript.sentences[0].model_copy(
        update={
            "sentence_id": "later",
            "text": "Authorize 30.0 systems.",
            "start_seconds": 10,
            "end_seconds": 20,
        }
    )
    source.transcript.sentences[0].end_seconds = 10
    source.transcript.sentences.append(row)
    text = "an introduction authorize 300 systems"
    assert _matched_numbers(source, text.index("300"), text.index("300") + 3) == [
        Decimal("30.0")
    ]


# Publisher-text controls; timings below are synthetic test intervals only.
@pytest.mark.parametrize("condition,clip_text,source_text", [('reordered', 'rather than expelled from our country applause our journey is not complete until all our children from the streets of detroit to the hills striving hopeful immigrants who still see america as a land of opportunity applause until bright young students and engineers are enlisted in our workforce', 'ted like anyone else under the law applause for if we are truly created equal then surely the love we commit to one another must be equal as well applause our journey is not complete until no citizen is forced to wait for hours to exercise the right to vote applause our journey is not complete until we find a better way to welcome the striving hopeful immigrants who still see america as a land of opportunity applause until bright young students and engineers are enlisted in our workforce rather than expelled from our country applause our journey is not complete until all our children from the streets of detroit to the hills of appalachia to the quiet lanes of newtown know that they are'), ('cross_event_splice', 'when a younger john lewis woke up that morning 50 years ago and made his way to brown chapel heroics were not on his he has shown throughout this transition forty four americans have now taken the presidential oath the words have been spoken during rising tides of', "my fellow citizens i stand here today humbled by the task before us grateful for the trust you've bestowed mindful of the sacrifices borne by our ancestors i thank president bush for his service to our nation applause as well as the generosity and cooperation he has shown throughout this transition forty four americans have now taken the presidential oath the words have been spoken during rising tides of prosperity and the still waters of peace yet every so often the oath is taken amidst gathering clouds and raging storms at these moments america has carried on not simply because of the skill or visi")])
def test_asymmetric_matching_half_does_not_verify_destructive_excerpt(condition, clip_text, source_text):
    score = fuzz.partial_ratio(normalize_text(clip_text), normalize_text(source_text))
    assert score >= 75
    result = build_alignment(features("clip", clip_text), features("source", source_text), score)
    assert result.status == "no_verified_match"
    assert any("remaining part is not supported" in item for item in result.limitations)


def test_uniform_asr_substitutions_preserve_fuzzy_source_recovery():
    text = " ".join("penguin" if i % 4 == 1 else word for i, word in enumerate(PASSAGE.split()))
    score = fuzz.partial_ratio(normalize_text(text), normalize_text(PASSAGE))
    assert score >= 75
    assert build_alignment(features("clip", text), features("source", PASSAGE), score).status == "verified"


def _cue_features(media_id, cues):
    return MediaFeatures.model_validate(
        {
            "media_id": media_id,
            "duration_seconds": max(end for _, end, _ in cues),
            "transcript": {
                "status": "partial",
                "sentences": [
                    {
                        "sentence_id": f"{media_id}_{index}",
                        "start_seconds": start,
                        "end_seconds": end,
                        "text": text,
                    }
                    for index, (start, end, text) in enumerate(cues)
                ],
            },
        }
    )


# Saved provider cues from the hosted YKjPI6no5ng failure. They preserve caption
# errors and the commentary interruption; no audio timing is inferred here.
_OBAMA_CLIP_CUES = [
    (1, 8, "if you've been successful you don't you didn't get there on your own you you didn't get there on your own"),
    (8, 14, "I'm always struck by people who think well it must be cuz I was just so smart there are a lot of smart people out"),
    (14, 25, "there it must be because I worked harder than everybody else let me tell you something there are a whole bunch of hardworking people out"),
    (25, 33, "there if you were successful somebody along the line gave you some help there was a great"),
    (33, 41, "teacher somewhere in your life somebody helped to create this unbelievable American system that we had"),
    (41, 49, "that allowed you to thrive somebody invested in roads and bridges if you got a business that you didn't build that"),
]
_OBAMA_COMMENTARY_CUES = [
    (168, 175, "didn't if you've been successful you don't you didn't get there on your own why right you you didn't get there"),
    (175, 182, "on your own I'm always struck by people who think well it must be cuz I was just so smart there are a lot of smart people"),
    (182, 194, "out there it must be because I worked harder than everybody else let me tell you something there a whole bunch of hardworking people out"),
    (194, 201, "there if you were successful somebody along the line gave you some help there was a"),
    (201, 210, "great teacher somewhere in your life somebody helped to create this unbelievable American System that we"),
    (210, 219, "have that allowed you to thrive wow so this is insane have you ever heard a president of the United"),
    (219, 227, "States of America speak in this way this is unheard of what a liar a deceiver"),
    (227, 235, "this is a desperate attempt this class Warfare he keep trying to create so the people who work hard sacrifice did"),
    (235, 243, "without struggle to build businesses then do it on their own you had teachers he goes on to say if you got a business"),
    (243, 253, "somebody else made that happen somebody invested in roads and bridges if you got a business government that you didn't build that somebody else made that"),
    (253, 260, "happen the internet didn't get invented on its own government research created the internet so then all the companies"),
]


def test_observed_conclusion_outside_commentary_match_rejects_one_passage():
    clip = _cue_features("clip", _OBAMA_CLIP_CUES)
    source = _cue_features("source", _OBAMA_COMMENTARY_CUES)
    clip_text = normalize_text(" ".join(cue[2] for cue in _OBAMA_CLIP_CUES))
    source_text = normalize_text(" ".join(cue[2] for cue in _OBAMA_COMMENTARY_CUES))
    score = fuzz.partial_ratio(clip_text, source_text)
    assert score >= 85
    result = build_alignment(clip, source, score)
    assert result.status == "no_verified_match"
    assert not result.matches
    assert any("ending of the submitted speech occurs outside" in item for item in result.limitations)


def test_same_observed_conclusion_inside_contiguous_cues_remains_verified():
    source_cues = [(start + 168, end + 168, text) for start, end, text in _OBAMA_CLIP_CUES]
    result = build_alignment(
        _cue_features("clip", _OBAMA_CLIP_CUES),
        _cue_features("source", source_cues),
        100,
    )
    assert result.status == "verified"
    assert result.matches[0].source_end_seconds == 217


def test_terminal_anchor_uses_enclosing_provider_cues_without_inventing_times():
    from backend.services.resolution.alignment import _terminal_anchor_outside_passage

    clip = normalize_text(" ".join(cue[2] for cue in _OBAMA_CLIP_CUES))
    source = "an introduction " + clip + " final remarks"
    assert not _terminal_anchor_outside_passage(clip, source, 16, len(source) - 14)


def test_unobserved_or_noisy_ending_is_not_mislocalized_without_outside_anchor():
    from backend.services.resolution.alignment import _terminal_anchor_outside_passage

    clip = normalize_text(" ".join(cue[2] for cue in _OBAMA_CLIP_CUES))
    source = normalize_text(" ".join(cue[2] for cue in _OBAMA_COMMENTARY_CUES[:7]))
    assert not _terminal_anchor_outside_passage(clip, source, 0, len(source))


def test_closing_anchor_inside_and_outside_proposed_cues_is_not_rejected():
    from backend.services.resolution.alignment import _terminal_anchor_outside_passage

    clip = normalize_text(" ".join(cue[2] for cue in _OBAMA_CLIP_CUES))
    closing_anchor = "somebody invested in roads and bridges if you"
    prefix = closing_anchor + " an introduction "
    source = prefix + clip + " final remarks " + closing_anchor
    assert not _terminal_anchor_outside_passage(
        clip, source, len(prefix), len(prefix) + len(clip)
    )
