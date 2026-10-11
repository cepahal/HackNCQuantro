"""Provider recovery must retain the application's transcript evidence checks."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_resolution_pipeline import CLIP, SOURCE, WORDS, comparison, media

from backend.services.resolution import analysis, run
from backend.services.shared.errors import ErrorCode, QuantroError, Stage


def _groq_response(value):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(value)),
    )])


def _gemini_response(value):
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value="STOP"))],
        text=json.dumps(value),
    )


@pytest.fixture
def language_providers(monkeypatch):
    """Exercise both real response adapters while preventing all network access."""
    monkeypatch.setenv("GROQ_API_KEY", "offline-test-only")
    monkeypatch.setenv("GEMINI_COMPARISON_MODEL", "")
    groq = Mock()
    groq.__enter__ = Mock(return_value=groq)
    groq.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(analysis, "Groq", Mock(return_value=groq))
    monkeypatch.setattr(analysis.httpx, "Client", Mock())
    monkeypatch.setattr(analysis.ssl, "create_default_context", Mock())
    gemini = Mock()
    fallback = Mock(return_value=(gemini, "offline-gemini-model"))
    monkeypatch.setattr(analysis, "client_and_model", fallback)
    return groq, gemini, fallback


@pytest.fixture
def transcript_context():
    post, clip = media("clip", CLIP)
    _, source = media("source", SOURCE, "candidate_1")
    context = run.build_context(run.build_alignment(clip, source, 96.3), source)
    assert context is not None
    return post, clip, context


def test_schema_invalid_groq_object_uses_valid_gemini_explanation(
    language_providers, transcript_context,
):
    groq, gemini, fallback = language_providers
    post, clip, _ = transcript_context
    groq.chat.completions.create.return_value = _groq_response({})
    gemini.models.generate_content.return_value = _gemini_response({
        "takeaways": "The speaker urges graduates to earn their success.",
        "meaning": "The address emphasizes responsibility and hard work.",
    })

    result = analysis.interpret_clip(post, clip)

    assert "The speaker urges graduates to earn their success." in result
    assert "responsibility and hard work" in result
    fallback.assert_called_once_with(Stage.COMPARE_CONTEXT_AND_CAPTION)
    assert gemini.models.generate_content.call_count == 1
    gemini.close.assert_called_once()
    assert groq.chat.completions.create.call_count >= 1


def test_valid_groq_explanation_does_not_call_gemini(
    language_providers, transcript_context,
):
    groq, gemini, fallback = language_providers
    post, clip, _ = transcript_context
    groq.chat.completions.create.return_value = _groq_response({
        "takeaways": "The speaker urges graduates to earn their success.",
        "meaning": "Hard work is presented as necessary for success.",
    })

    result = analysis.interpret_clip(post, clip)

    assert "Hard work is presented as necessary for success." in result
    assert groq.chat.completions.create.call_count == 1
    fallback.assert_not_called()
    gemini.models.generate_content.assert_not_called()


def test_invalid_groq_citation_uses_validated_gemini_comparison(
    language_providers, transcript_context,
):
    groq, gemini, fallback = language_providers
    post, clip, context = transcript_context
    bad = comparison().model_dump(mode="json")
    bad["findings"][0]["evidence"][0]["quote"] = "Invented speech never supplied."
    good = comparison().model_dump(mode="json")
    # A provider's claimed provenance must not control the saved method.
    good["assessment_confidence"]["method"] = "groq_transcript_context"
    good["findings"][0]["evidence"][0].update(start_seconds=0.0, end_seconds=1.0)
    groq.chat.completions.create.return_value = _groq_response(bad)
    gemini.models.generate_content.return_value = _gemini_response(good)

    result = analysis.compare_clip(post, clip, context)

    fallback.assert_called_once_with(Stage.COMPARE_CONTEXT_AND_CAPTION)
    assert result.assessment_confidence.method == "gemini_transcript_context"
    evidence = result.findings[0].evidence[0]
    assert evidence.quote == WORDS
    assert evidence.sentence_ids == ["source_1"]
    assert (evidence.start_seconds, evidence.end_seconds) == (100.0, 112.0)
    assert "Invented speech" not in result.model_dump_json()
    gemini.close.assert_called_once()


@pytest.mark.parametrize("invalid", ["fabricated_quote", "unknown_sentence", "unknown_media"])
def test_invalid_citations_from_both_providers_cannot_be_accepted(
    language_providers, transcript_context, invalid,
):
    groq, gemini, _ = language_providers
    post, clip, context = transcript_context
    bad = comparison().model_dump(mode="json")
    evidence = bad["findings"][0]["evidence"][0]
    if invalid == "fabricated_quote":
        evidence["quote"] = "Invented speech never supplied."
    elif invalid == "unknown_sentence":
        evidence["sentence_ids"] = ["source_sentence_does_not_exist"]
    else:
        evidence["media_id"] = "unrelated_recording"
    groq.chat.completions.create.return_value = _groq_response(bad)
    gemini.models.generate_content.return_value = _gemini_response(bad)

    with pytest.raises(QuantroError) as caught:
        analysis.compare_clip(post, clip, context)

    assert caught.value.code is ErrorCode.PROCESSING_FAILED
    assert caught.value.stage is Stage.COMPARE_CONTEXT_AND_CAPTION
    assert groq.chat.completions.create.call_count >= 1
    assert gemini.models.generate_content.call_count >= 1
    assert "Invented speech" not in caught.value.message
    assert gemini.close.call_count >= 1


def test_valid_groq_comparison_retains_primary_provider_and_evidence(
    language_providers, transcript_context,
):
    groq, gemini, fallback = language_providers
    post, clip, context = transcript_context
    groq.chat.completions.create.return_value = _groq_response(
        comparison().model_dump(mode="json"),
    )

    result = analysis.compare_clip(post, clip, context)

    assert result.assessment_confidence.method == "groq_transcript_context"
    assert result.findings[0].evidence[0].quote == WORDS
    assert groq.chat.completions.create.call_count == 1
    fallback.assert_not_called()
    gemini.models.generate_content.assert_not_called()
