"""Bounded native-video fallback and conservative pipeline gates; no live calls."""

import copy
import json
import re
from types import SimpleNamespace

import pytest

import backend.services.pipeline.run_ingest as ingest
import backend.services.reporting.remote_youtube as remote
import backend.services.reporting.run_analysis as analysis
from backend.services.external_apis.retrieve_post import RetrievalError
from backend.services.shared.errors import ErrorCode, QuantroError, Stage
from backend.services.shared.types import ReportV2
from backend.services.storage.tiger import TigerStore

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SENTENCES = [
    {"start_seconds": 0.0, "end_seconds": 2.0, "text": "Here we are at the zoo."},
    {"start_seconds": 2.0, "end_seconds": 5.0, "text": "Behind me are the elephants."},
]


class FakeModels:
    def __init__(self):
        self.count = 2000
        self.count_error = None
        self.generation_error = None
        self.payload = {"sentences": copy.deepcopy(SENTENCES)}
        self.raw_text = None
        self.finish = "STOP"
        self.usage = SimpleNamespace(
            prompt_token_count=2000,
            prompt_tokens_details=[SimpleNamespace(modality="VIDEO", token_count=500)],
        )
        self.calls = []

    def count_tokens(self, **kwargs):
        self.calls.append(("count", kwargs))
        if self.count_error:
            raise self.count_error
        return SimpleNamespace(total_tokens=self.count)

    def generate_content(self, **kwargs):
        self.calls.append(("generate", kwargs))
        if self.generation_error:
            raise self.generation_error
        return SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=self.finish)],
            text=self.raw_text if self.raw_text is not None else json.dumps(self.payload),
            usage_metadata=self.usage,
        )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(remote, "_youtube_metadata", lambda url: {})
    models = FakeModels()
    state = SimpleNamespace(models=models, closed=False)

    def close():
        state.closed = True

    state.close = close
    monkeypatch.setattr(remote, "_client_and_model", lambda: (state, "gemini-3.1-flash-lite"))
    return state


def _walk(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk(nested)


def test_native_video_transcript_is_bounded_provenanced_and_honestly_partial(client):
    report = remote.build_remote_youtube_report("job_remote", URL)

    assert isinstance(report, ReportV2)
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    requests = [args for _, args in client.models.calls]
    assert all(args["model"] == "gemini-3.1-flash-lite" for args in requests)
    assert requests[0]["contents"] == requests[1]["contents"]
    video_parts = [part for part in _walk(requests[1]["contents"]) if part.get("file_data")]
    assert len(video_parts) == 1
    assert video_parts[0]["file_data"]["file_uri"] == URL
    assert video_parts[0]["file_data"]["mime_type"] == "video/mp4"
    metadata = video_parts[0]["video_metadata"]
    assert metadata["end_offset"] in {"60s", "PT1M", "PT60S"}
    assert 0 < float(metadata["fps"]) <= 0.5
    data = report.model_dump(mode="json")
    assert data["schema_version"] == "2.1"
    assert data["outcome"] == "no_verified_match"
    assert all(data[field] is None for field in ("source", "alignment", "context", "comparison"))
    submitted = data["submitted"]
    assert submitted["post"]["post_url"] == URL
    assert "gemini" in submitted["post"]["retrieval"]["provider"].lower()
    assert URL in submitted["post"]["retrieval"]["metadata_source_urls"]
    assert submitted["post"]["media"]["duration_seconds"] is None
    assert re.fullmatch(r"[A-Za-z0-9_-]+", submitted["post"]["media"]["media_id"]) is None
    assert submitted["post"]["published_at"] is None
    assert submitted["post"]["author"]["followers_count"] is None
    assert submitted["transcript"]["status"] == "partial"
    assert [row["text"] for row in submitted["transcript"]["sentences"]] == [row["text"] for row in SENTENCES]
    ranges = submitted["transcript"]["processed_ranges"]
    assert ranges
    assert min(row["start_seconds"] for row in ranges) == 0.0
    assert max(row["end_seconds"] for row in ranges) == 5.0
    assert all(0 <= row["start_seconds"] < row["end_seconds"] <= 5.0 for row in ranges)
    assert all(any(row["start_seconds"] <= sentence["start_seconds"] and row["end_seconds"] >= sentence["end_seconds"]
                   for row in ranges) for sentence in SENTENCES)
    assert submitted["frames"] == submitted["visual_text"] == []
    limitations = " ".join(data["limitations"]).lower()
    assert "estimat" in limitations
    assert "download" in limitations or "bytes" in limitations
    assert "search" in limitations
    assert "authentic" in limitations
    serialized = json.dumps(data)
    assert all(marker not in serialized for marker in ("local_path", "/tmp/", "file://"))
    assert client.closed


@pytest.mark.parametrize("count", [12001, 0, -1, None, True, "2000"])
def test_input_token_guard_fails_before_generation(count, client):
    client.models.count = count

    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_token_guard", URL)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert [name for name, _ in client.models.calls] == ["count"]
    assert client.closed


def test_token_budget_boundary_and_audio_modality_remain_usable(client):
    client.models.count = 12000
    client.models.usage = SimpleNamespace(
        prompt_token_count=12000,
        prompt_tokens_details=[SimpleNamespace(modality="AUDIO", token_count=500)],
    )

    report = remote.build_remote_youtube_report("job_budget_boundary", URL)

    assert report.submitted.transcript.sentences
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    assert client.closed


@pytest.mark.parametrize("payload", [
    {},
    {"sentences": []},
    {"sentences": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": " "}]},
    {"sentences": [{"start_seconds": -1.0, "end_seconds": 1.0, "text": "Invalid start."}]},
    {"sentences": [{"start_seconds": 0.0, "end_seconds": 61.0, "text": "Beyond bounded segment."}]},
    {"sentences": [{"start_seconds": 1.0, "end_seconds": 1.0, "text": "No duration."}]},
    {"sentences": [{"start_seconds": False, "end_seconds": 1.0, "text": "Boolean timestamp."}]},
    {"sentences": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "x" * 16001}]},
    {"sentences": [SENTENCES[1], SENTENCES[0]]},
    {"sentences": [SENTENCES[0], {"start_seconds": 1.0, "end_seconds": 3.0, "text": "Overlapping."}]},
    {"sentences": [{"start_seconds": float("nan"), "end_seconds": 1.0, "text": "Nonfinite start."}]},
    {"sentences": [{"start_seconds": 0.0, "end_seconds": float("inf"), "text": "Nonfinite end."}]},
    {"sentences": copy.deepcopy(SENTENCES), "invented_author": "A guessed speaker"},
    {"sentences": [{**SENTENCES[0], "local_path": "/tmp/invented.mp4"}]},
    {"sentences": [{"start_seconds": float(index), "end_seconds": float(index + 1), "text": "x" * 2000} for index in range(9)]},
    {"sentences": [{"start_seconds": index / 10, "end_seconds": (index + 1) / 10, "text": "word"} for index in range(151)]},
])
def test_malformed_or_unusable_transcript_never_becomes_successful_report(payload, client):
    client.models.payload = payload

    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_bad_response", URL)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    assert client.closed


@pytest.mark.parametrize("finish,text", [("MAX_TOKENS", "{}"), ("SAFETY", "{}"), ("STOP", "not-json"), ("STOP", "")])
def test_incomplete_or_invalid_json_is_structured_failure(finish, text, client):
    client.models.finish, client.models.raw_text = finish, text

    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_incomplete", URL)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert len(client.models.calls) == 2
    assert client.closed


@pytest.mark.parametrize("usage", [
    None,
    SimpleNamespace(prompt_token_count=12001, prompt_tokens_details=[]),
    SimpleNamespace(prompt_token_count=2000, prompt_tokens_details=[]),
    SimpleNamespace(prompt_token_count=2000, prompt_tokens_details=[SimpleNamespace(modality="TEXT", token_count=2000)]),
    SimpleNamespace(prompt_token_count=2000, prompt_tokens_details=[SimpleNamespace(modality="VIDEO", token_count=0)]),
])
def test_missing_media_usage_proof_rejects_text_only_url_answers(usage, client):
    client.models.usage = usage

    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_ungrounded", URL)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert client.closed


def test_client_cleanup_failure_preserves_validated_evidence_without_leaking(client, caplog):
    def fail_close():
        raise RuntimeError("sample-cleanup-secret")

    client.close = fail_close
    report = remote.build_remote_youtube_report("job_cleanup_failure", URL)

    assert report.submitted.transcript.sentences
    assert "sample-cleanup-secret" not in caplog.text


class ProviderFailure(RuntimeError):
    code = 429


@pytest.mark.parametrize("request_stage", ["count", "generate"])
@pytest.mark.parametrize("exception", [ProviderFailure("secret=sample-provider-secret"), TimeoutError("https://signed.example/?token=sample-provider-secret")])
def test_provider_failures_are_bounded_redacted_structured_errors(request_stage, exception, client, caplog):
    if request_stage == "count":
        client.models.count_error = exception
    else:
        client.models.generation_error = exception

    with pytest.raises(QuantroError) as caught:
        remote.build_remote_youtube_report("job_provider_failure", URL)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert caught.value.retryable is True
    assert len(client.models.calls) == (1 if request_stage == "count" else 2)
    assert "sample-provider-secret" not in str(caught.value.public_dict()) + caplog.text
    assert client.closed


@pytest.mark.parametrize("url", ["not-a-url", "https://127.0.0.1/video.mp4", "https://www.instagram.com/reel/example/", "https://www.youtube.com/watch?v=short"])
def test_standalone_remote_fallback_rejects_invalid_or_non_youtube_inputs(url, client):
    with pytest.raises(QuantroError):
        remote.build_remote_youtube_report("job_invalid", url)
    assert client.models.calls == []


@pytest.fixture
def pipeline_store(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REPORT_BASE_URL", "https://quantro.test")
    store = TigerStore(f"sqlite:///{tmp_path / 'remote-fallback.db'}")
    store.ensure_schema()
    monkeypatch.setattr(analysis, "check_search_settings", lambda: None)
    yield store
    store.close()


def _fallback_spy(monkeypatch, client):
    monkeypatch.setattr(analysis, "_discover_remote_sources", lambda report, store, **kwargs: report)
    calls = []
    original = remote.build_remote_youtube_report

    def build(job_id, url):
        calls.append((job_id, url))
        return original(job_id, url)

    monkeypatch.setattr(remote, "build_remote_youtube_report", build)
    monkeypatch.setattr(analysis, "build_remote_youtube_report", build)
    return calls


def test_only_classified_youtube_challenge_recovers_and_saves_idempotently(
    monkeypatch, client, pipeline_store
):
    job_id = "job_recover_remote"
    pipeline_store.upsert_job(job_id, URL, "running")
    calls = _fallback_spy(monkeypatch, client)

    def blocked(*args, **kwargs):
        pipeline_store.set_job_status(job_id, "failed", "MEDIA_UNAVAILABLE", "retrieve_post")
        raise RetrievalError("MEDIA_UNAVAILABLE", "Platform challenge.", reason="platform_challenge")

    monkeypatch.setattr(analysis, "run_ingest", blocked)
    result = analysis.run_analysis(job_id, URL, store=pipeline_store)
    again = analysis.run_analysis(job_id, URL, store=pipeline_store)

    assert result.outcome == "no_verified_match"
    assert result.report_id == again.report_id
    assert calls == [(job_id, URL)]
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    assert pipeline_store.get_job(job_id)["status"] == "completed"
    assert len(pipeline_store.get_job_reports(job_id)) == 1
    saved = pipeline_store.get_report(result.report_id)
    assert saved["submitted"]["transcript"]["sentences"]
    assert saved["source"] is saved["alignment"] is saved["comparison"] is None


def test_deferred_youtube_challenge_stays_running_until_remote_report_is_saved(
    monkeypatch, client, pipeline_store
):
    monkeypatch.setattr(analysis, "_discover_remote_sources", lambda report, store, **kwargs: report)
    job_id = "job_remote_no_failed_poll_race"
    original = remote.build_remote_youtube_report
    states = []

    def blocked_retrieval(*args, **kwargs):
        raise RetrievalError("MEDIA_UNAVAILABLE", "Platform challenge.", reason="platform_challenge")

    def observe_fallback(job, url):
        state = pipeline_store.get_job(job)
        states.append(state["status"])
        assert state["post_json"] is state["features_json"] is None
        return original(job, url)

    monkeypatch.setattr(ingest, "retrieve_post", blocked_retrieval)
    monkeypatch.setattr(analysis, "build_remote_youtube_report", observe_fallback)
    result = analysis.run_analysis(job_id, URL, store=pipeline_store)

    assert states == ["running"]
    assert pipeline_store.get_job(job_id)["status"] == "completed"
    assert pipeline_store.get_report(result.report_id) is not None


@pytest.mark.parametrize("url,code,reason,wrapped", [
    (URL, "MEDIA_UNAVAILABLE", "restricted_video", False),
    (URL, "MEDIA_UNAVAILABLE", "unavailable_video", False),
    (URL, "MEDIA_UNAVAILABLE", "provider_rate_limit", False),
    (URL, "MEDIA_UNAVAILABLE", "access_blocked", False),
    (URL, "MEDIA_UNAVAILABLE", "unsupported_format", False),
    (URL, "MEDIA_UNAVAILABLE", "download_failed", False),
    (URL, "STORAGE_FAILED", "platform_challenge", False),
    (URL, "INVALID_URL", "platform_challenge", False),
    ("https://www.instagram.com/reel/example/", "MEDIA_UNAVAILABLE", "platform_challenge", False),
    (URL, "MEDIA_UNAVAILABLE", "platform_challenge", True),
])
def test_other_failure_types_never_invoke_remote_fallback(
    url, code, reason, wrapped, monkeypatch, client, pipeline_store
):
    job_id = "job_no_remote"
    pipeline_store.upsert_job(job_id, url, "running")
    calls = _fallback_spy(monkeypatch, client)

    def fail(*args, **kwargs):
        error = RetrievalError(code, "Safe failure.", reason=reason)
        if wrapped:
            raise QuantroError(code, Stage.RETRIEVE_POST) from error
        raise error

    monkeypatch.setattr(analysis, "run_ingest", fail)
    with pytest.raises(QuantroError) as caught:
        analysis.run_analysis(job_id, url, store=pipeline_store)

    assert caught.value.code == code
    assert calls == client.models.calls == []
    assert pipeline_store.get_job_reports(job_id) == []


def test_failed_remote_provider_keeps_job_failed_without_saving_report(
    monkeypatch, client, pipeline_store
):
    job_id = "job_remote_fails"
    pipeline_store.upsert_job(job_id, URL, "running")
    _fallback_spy(monkeypatch, client)
    client.models.generation_error = ProviderFailure("sample-provider-secret")

    def blocked(*args, **kwargs):
        raise RetrievalError("MEDIA_UNAVAILABLE", "Platform challenge.", reason="platform_challenge")

    monkeypatch.setattr(analysis, "run_ingest", blocked)
    with pytest.raises(QuantroError) as caught:
        analysis.run_analysis(job_id, URL, store=pipeline_store)

    assert caught.value.code == ErrorCode.TRANSCRIPTION_FAILED
    assert pipeline_store.get_job(job_id)["status"] == "failed"
    assert pipeline_store.get_job_reports(job_id) == []


def test_visual_discovery_clues_share_one_call_and_stay_private(client):
    client.models.payload["visual_clues"] = ["OCR: PBS NEWS HOUR", "Scene: A podium beside a crowd holding signs."]
    hints = []
    report = remote.build_remote_youtube_report("job_visual", URL, discovery_hints=hints)
    assert hints == client.models.payload["visual_clues"]
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    assert "visual_clues" not in report.model_dump_json() and "PBS NEWS HOUR" not in report.model_dump_json()
    assert [row.text for row in report.submitted.transcript.sentences] == [row["text"] for row in SENTENCES]


@pytest.mark.parametrize("clues", [["OCR: x"] * 5, ["OCR: " + "x" * 160], ["https://example.com"], ["Scene: api_key=private"], ["Scene: <script>"], ["Inferred: Someone in 2020"], None, 42, "OCR: NEWS", [42]])
def test_invalid_visual_clues_never_escape_provider_boundary(client, clues):
    client.models.payload["visual_clues"] = clues
    hints = []
    report = remote.build_remote_youtube_report("job_bad_visual", URL, discovery_hints=hints)
    assert hints == []
    assert [row.text for row in report.submitted.transcript.sentences] == [row["text"] for row in SENTENCES]


def test_visual_clues_do_not_replace_missing_speech(client):
    client.models.payload = {"sentences": [], "visual_clues": ["OCR: NEWS"]}
    hints = []
    with pytest.raises(QuantroError):
        remote.build_remote_youtube_report("job_silent_visual", URL, discovery_hints=hints)
    assert hints == []


def test_visual_clues_require_confirmed_video_modality(client):
    client.models.payload["visual_clues"] = ["OCR: NEWS"]
    client.models.usage.prompt_tokens_details = [SimpleNamespace(modality="AUDIO", token_count=500)]
    hints = []
    report = remote.build_remote_youtube_report("job_no_visual_usage", URL, discovery_hints=hints)
    assert hints == [] and report.submitted.transcript.sentences
