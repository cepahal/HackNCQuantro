"""Incomplete caption fragments must not turn into model-written scene verdicts."""

from unittest.mock import Mock

import pytest

from backend.services.resolution import analysis, media
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import (
    Author,
    Engagement,
    MediaAsset,
    Post,
    Retrieval,
)


def clip(text="as damon simon", *, status="partial", title=None, untimed=""):
    post = Post(
        post_id="post_sparse", post_url="https://www.youtube.com/watch?v=rQ6N-sb7SVQ",
        platform="youtube", title=title, author=Author(), engagement=Engagement(),
        media=MediaAsset(media_id="clip_sparse", local_path="", mime_type="video/mp4"),
        retrieval=Retrieval(retrieved_at="2026-10-11T12:00:00Z", provider="captions"),
    )
    features = media._ResolvedFeatures(
        media_id="clip_sparse", duration_seconds=13.0,
        transcript={
            "status": status,
            "processed_ranges": [{"start_seconds": 4.0, "end_seconds": 13.0}] if text else [],
            "sentences": [{"sentence_id": "s1", "start_seconds": 4.0,
                           "end_seconds": 13.0, "text": text, "speaker_id": None}] if text else [],
        },
    )
    features._full_transcript_text = (text + " " + untimed).strip()
    return post, features


def test_live_bush_caption_fragment_does_not_call_model_or_invent_quotation_claims(monkeypatch):
    provider = Mock(side_effect=AssertionError("A fragment cannot support semantic analysis"))
    monkeypatch.setattr(analysis, "_json_response", provider)
    post, features = clip(
        title='George W. Bush "Fool me once, shame on, shame on you."',
        untimed="Esto es una frase sin intervalo observado que no debe ampliar la evidencia temporal.",
    )
    summary = analysis.interpret_clip(post, features)
    provider.assert_not_called()
    assert 'The available transcript contains: "as damon simon"' in summary
    assert "The uploaded title says" in summary and "This is a search clue" in summary
    assert "too limited for a reliable interpretation" in summary
    assert "Esto es" not in summary
    assert not any(word in summary.lower() for word in ["misquoted", "altered", "joking", "mistaken", "misleading", "edited"])
    assert summary.startswith("Takeaways:")
    assert "What this means:" in summary and "Reliability:" in summary


@pytest.mark.parametrize("status", ["partial", "unavailable"])
def test_no_timed_speech_gives_availability_explanation_without_invented_quote(monkeypatch, status):
    provider = Mock(side_effect=AssertionError("No observed words"))
    monkeypatch.setattr(analysis, "_json_response", provider)
    post, features = clip("", status=status, untimed="An untimed provider clue only.")
    summary = analysis.interpret_clip(post, features)
    provider.assert_not_called()
    assert "Too little usable speech was retrieved" in summary
    assert "An untimed provider clue" not in summary
    assert "The uploaded title says" not in summary


def test_complete_brief_action_keeps_normal_analysis(monkeypatch):
    provider = Mock(return_value={"takeaways": "He orders coffee.", "meaning": "The request is an everyday food order."})
    monkeypatch.setattr(analysis, "_json_response", provider)
    post, features = clip("Coffee, please.", status="complete", title="A cafe visit")
    summary = analysis.interpret_clip(post, features)
    provider.assert_called_once()
    assert "He orders coffee" in summary
    assert "too limited for a reliable interpretation" not in summary


def test_partial_milkshake_request_keeps_concrete_analysis(monkeypatch):
    provider = Mock(return_value={"takeaways": "He asks for 30 milkshakes.", "meaning": "The request concerns a large food order."})
    monkeypatch.setattr(analysis, "_json_response", provider)
    post, features = clip("So can I have 30 milkshakes?", title="A restaurant order")
    summary = analysis.interpret_clip(post, features)
    provider.assert_called_once()
    assert "He asks for 30 milkshakes" in summary


def test_long_non_space_delimited_speech_keeps_normal_analysis(monkeypatch):
    text = "我们今天在这里讨论教育和公共服务的发展并且希望每个人都能得到公平机会而不是根据片段猜测讲话的意思"
    provider = Mock(return_value={"takeaways": "The passage discusses education and public services.", "meaning": "It calls for fair opportunities."})
    monkeypatch.setattr(analysis, "_json_response", provider)
    post, features = clip(text)
    analysis.interpret_clip(post, features)
    provider.assert_called_once()


def test_sparse_title_format_is_safe_and_does_not_assert_identity(monkeypatch):
    monkeypatch.setattr(analysis, "_json_response", Mock(side_effect=AssertionError("No model")))
    post, features = clip(title="**George W. Bush**\u2014a familiar quotation")
    summary = analysis.interpret_clip(post, features)
    assert "The uploaded title says" in summary
    assert "**" not in summary and "\u2014" not in summary


def test_unsafe_title_is_omitted_without_losing_available_words(monkeypatch):
    monkeypatch.setattr(analysis, "_json_response", Mock(side_effect=AssertionError("No model")))
    post, features = clip(title="<script>private instructions</script>")
    summary = analysis.interpret_clip(post, features)
    assert "<script>" not in summary and "private instructions" not in summary
    assert "as damon simon" in summary


def test_normal_analysis_still_propagates_provider_error(monkeypatch):
    error = QuantroError(ErrorCode.PROCESSING_FAILED, Stage.COMPARE_CONTEXT_AND_CAPTION)
    monkeypatch.setattr(analysis, "_json_response", Mock(side_effect=error))
    post, features = clip("The passage discusses the importance of education and public services.")
    with pytest.raises(QuantroError) as caught:
        analysis.interpret_clip(post, features)
    assert caught.value is error
