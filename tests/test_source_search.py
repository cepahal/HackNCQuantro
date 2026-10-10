"""Contracts and failure boundaries across source-search stages 3, 4 and 5."""

import copy
import importlib
import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from backend.services.external_apis.build_search_queries import build_search_queries
from backend.services.shared.types import Post
from backend.services.external_apis.retrieve_candidate_media import (
    retrieve_candidate_media,
)
from backend.services.external_apis.search_candidates import search_candidates
from backend.services.shared.types import Candidate
from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)
from backend.services.shared.errors import QuantroError

QUERY = '"those numbers were accurate"'
URL = "https://www.youtube.com/watch?v=full123"


@pytest.fixture
def post() -> Post:
    return {
        "post_id": "submitted",
        "candidate_id": None,
        "post_url": URL,
        "platform": "youtube",
        "title": "Example Interview",
        "caption": "Canceling the program",
        "published_at": None,
        "author": {
            "id": None,
            "handle": "interviewer",
            "display_name": None,
            "profile_url": None,
            "bio": None,
            "verified": None,
            "followers_count": None,
            "following_count": None,
            "posts_count": None,
        },
        "engagement": {
            "views_count": None,
            "likes_count": None,
            "comments_count": None,
            "shares_count": None,
        },
        "media": {
            "media_id": "clip",
            "local_path": "/unused.mp4",
            "mime_type": "video/mp4",
            "playback_url": None,
            "duration_seconds": 14.0,
            "width": None,
            "height": None,
            "fps": None,
            "has_audio": True,
        },
        "retrieval": {
            "retrieved_at": "2026-10-10T17:00:00Z",
            "provider": "test",
            "metadata_source_urls": [URL],
            "unavailable_fields": [],
            "warnings": [],
        },
    }


@pytest.fixture
def features() -> dict[str, Any]:
    return {
        "media_id": "clip",
        "transcript": {
            "status": "complete",
            "sentences": [
                {"text": "Thank you very much"},
                {"text": "If those numbers were accurate"},
                {"text": "If those numbers were accurate"},
            ],
        },
        "visual_text": [{"text": "Example Interview"}],
    }


@pytest.fixture
def candidate() -> Candidate:
    return {
        "candidate_id": "candidate_001",
        "url": URL,
        "title": "Search title",
        "publisher": None,
        "published_at": None,
        "snippet": "Unverified snippet",
        "discovered_by_queries": [QUERY],
    }


def test_queries_use_available_evidence_deduplicate_and_preserve_inputs(
    features: dict[str, Any], post: Post
) -> None:
    before = copy.deepcopy((features, post))
    result = build_search_queries(features, post)
    assert set(result) == {"queries", "warnings"}
    assert '"If those numbers were accurate"' in result["queries"]
    assert result["queries"].count('"Example Interview"') == 1
    assert '"Canceling the program"' in result["queries"]
    assert any("unverified" in w for w in result["warnings"])
    assert (features, post) == before
    assert json.loads(json.dumps(result)) == result


def test_unavailable_transcript_is_not_used(
    features: dict[str, Any], post: Post
) -> None:
    features["transcript"]["status"] = "unavailable"
    assert (
        '"If those numbers were accurate"'
        not in build_search_queries(features, post)["queries"]
    )


def test_empty_clues_are_normal(features: dict[str, Any], post: Post) -> None:
    features["transcript"]["sentences"] = []
    features["visual_text"] = []
    post["caption"] = post["title"] = None
    result = build_search_queries(features, post)
    assert result["queries"] == [] and result["warnings"]


def test_query_count_and_lengths_are_bounded(
    features: dict[str, Any], post: Post
) -> None:
    features["transcript"]["sentences"] = [
        {"text": f"Distinctive phrase number {n} about public transportation"}
        for n in range(600)
    ]
    features["visual_text"] = [{"text": f"Interview series {n}"} for n in range(10)]
    result = build_search_queries(features, post)
    assert len(result["queries"]) <= 8
    assert all(len(query) <= 512 for query in result["queries"])
    assert any("bounded" in warning for warning in result["warnings"])


@pytest.mark.parametrize("change", ["media_id", "transcript", "visual_text"])
def test_invalid_features_have_stage_error(
    features: dict[str, Any], post: Post, change: str
) -> None:
    features[change] = "wrong"
    with pytest.raises(QuantroError) as caught:
        build_search_queries(features, post)
    assert caught.value.to_dict()["stage"] == "build_search_queries"


class Provider:
    def __init__(
        self, rows: list[tuple[object, object]], failure: Exception | None = None
    ) -> None:
        self.rows = rows
        self.failure = failure
        self.calls: list[tuple[list[str], int]] = []
        self.closed = False

    def find_posts(self, queries: list[str], limit: int) -> list[tuple[object, object]]:
        self.calls.append((queries, limit))
        if self.failure:
            raise self.failure
        return self.rows

    def close(self) -> None:
        self.closed = True


def test_search_exact_contract_provenance_and_stable_ids(post: Post) -> None:
    provider = Provider([(post, [QUERY, '"Example Interview"'])])
    result = search_candidates(
        {
            "queries": [QUERY, QUERY, '"Example Interview"'],
            "warnings": ["caption unverified"],
        },
        5,
        provider=provider,
    )
    item = result["candidates"][0]
    assert set(item) == {
        "candidate_id",
        "url",
        "title",
        "publisher",
        "published_at",
        "snippet",
        "discovered_by_queries",
    }
    assert item["publisher"] == "interviewer" and item["published_at"] is None
    assert item["discovered_by_queries"] == [QUERY, '"Example Interview"']
    assert result["warnings"] == ["caption unverified"]
    assert provider.calls == [([QUERY, '"Example Interview"'], 5)]
    assert not provider.closed
    again = search_candidates([QUERY], 5, provider=Provider([(post, [QUERY])]))
    assert again["candidates"][0]["candidate_id"] == item["candidate_id"]
    assert json.loads(json.dumps(result)) == result


def test_search_empty_success_and_no_queries_do_not_require_cloud() -> None:
    assert search_candidates([QUERY], 5, provider=Provider([])) == {
        "candidates": [],
        "warnings": [],
    }
    assert search_candidates([], 5)["candidates"] == []


@pytest.mark.parametrize(
    "queries,limit",
    [
        ([QUERY], True),
        ([QUERY], 0),
        ([QUERY], 51),
        ([""], 5),
        ([None], 5),
        (["x" * 513], 5),
        ([QUERY] * 21, 5),
        ({"queries": [], "warnings": "bad"}, 5),
    ],
)
def test_search_input_validation(queries: Any, limit: Any) -> None:
    with pytest.raises(QuantroError) as caught:
        search_candidates(queries, limit, provider=Provider([]))
    assert caught.value.to_dict()["code"] == "SEARCH_FAILED"


@pytest.mark.parametrize(
    "error", [RuntimeError("secret DSN"), TimeoutError("secret token")]
)
def test_search_failure_is_not_empty_success(error: Exception) -> None:
    with pytest.raises(QuantroError) as caught:
        search_candidates([QUERY], 5, provider=Provider([], error))
    assert "secret" not in str(caught.value)
    assert caught.value.retryable == isinstance(error, TimeoutError)


def test_default_search_owns_connection(
    monkeypatch: pytest.MonkeyPatch, post: Post
) -> None:
    monkeypatch.setattr("backend.services.external_apis.search_candidates._environment", lambda: {"SEARCH_PROVIDER": "tiger"})
    provider = Provider([(post, [QUERY])])
    monkeypatch.setattr(TigerDataPostStore, "from_env", lambda: provider)
    result = search_candidates([QUERY], 1)
    assert provider.closed and result["candidates"]
    assert any("corpus only" in w for w in result["warnings"])


@pytest.mark.parametrize(
    "mutation", ["private_url", "bad_date", "false_provenance", "duplicate"]
)
def test_invalid_provider_records_are_errors(post: Post, mutation: str) -> None:
    matched = [QUERY]
    if mutation == "private_url":
        post["post_url"] = "http://127.0.0.1/a.mp4"
    if mutation == "bad_date":
        post["published_at"] = "invented"
    if mutation == "false_provenance":
        matched = ["not searched"]
    rows: list[tuple[object, object]] = [(post, matched)]
    if mutation == "duplicate":
        rows *= 2
    with pytest.raises(QuantroError):
        search_candidates([QUERY], 5, provider=Provider(rows))


class Cursor:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...]) -> object:
        self.connection.calls.append((sql, parameters))
        if self.connection.fail:
            raise RuntimeError("secret SQL DSN")
        return self

    def fetchone(self) -> tuple[object, ...]:
        return (self.connection.calls[-1][1][0],)

    def fetchall(self) -> list[tuple[object, object]]:
        return self.connection.rows

    def close(self) -> None:
        self.connection.cursor_closed = True


class Connection:
    autocommit = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[tuple[object, object]] = []
        self.fail = False
        self.rollbacks = 0
        self.commits = 0
        self.closed = False
        self.cursor_closed = False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_tiger_search_parameters_are_bound_and_read_transaction_ends(
    post: Post,
) -> None:
    connection = Connection()
    connection.rows = [(post, [QUERY])]
    query = "'; DROP TABLE posts; --"
    assert TigerDataPostStore(connection).find_posts([query], 5) == connection.rows
    sql, parameters = connection.calls[0]
    assert query not in sql and parameters == ([query], 5)
    assert "SELECT r.metadata, g.discovered" in sql and "media_bytes" not in sql
    assert (
        connection.rollbacks == 1
        and connection.commits == 0
        and connection.cursor_closed
    )
    assert not connection.closed


def test_tiger_search_failure_is_redacted() -> None:
    connection = Connection()
    connection.fail = True
    with pytest.raises(StorageError) as caught:
        TigerDataPostStore(connection).find_posts([QUERY], 5)
    assert "secret" not in str(caught.value)
    assert connection.rollbacks and connection.cursor_closed


def downloader(args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
    image = "--direct-image" in args
    path = (
        Path(args[args.index("--output-file") + 1])
        if image
        else Path(args[args.index("--output") + 1]).parent / "media.mp4"
    )

    def box(kind: bytes, content: bytes) -> bytes:
        return struct.pack(">I", len(content) + 8) + kind + content

    content = (
        b"\xff\xd8\xff\xe0test\xff\xd9"
        if image
        else box(b"ftyp", b"isom0000") + box(b"mdat", b"video") + box(b"moov", b"")
    )
    path.write_bytes(content)
    info: dict[str, object] = {
        "_type": "image" if image else "video",
        "description": "Actual downloaded description",
        "ext": "jpg" if image else "mp4",
        "protocol": "https",
        "vcodec": "avc1",
        "acodec": "mp4a",
    }
    return subprocess.CompletedProcess(args, 0, json.dumps(info), "")


@pytest.mark.parametrize("image", [False, True])
def test_candidate_download_persists_linked_post_before_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: Candidate, image: bool
) -> None:
    module = importlib.import_module("backend.services.external_apis.retrieve_post")
    monkeypatch.setattr(module.subprocess, "run", downloader)
    connection = Connection()
    if image:
        candidate["url"] = "https://example.com/recording.jpg"
    before = copy.deepcopy(candidate)
    result = retrieve_candidate_media(
        candidate, storage=TigerDataPostStore(connection), output_dir=tmp_path
    )
    metadata = connection.calls[0][1][6]
    assert isinstance(metadata, str) and json.loads(metadata) == result
    assert result["candidate_id"] == candidate["candidate_id"]
    assert result["caption"] == "Actual downloaded description"
    assert result["post_id"] != candidate["candidate_id"]
    assert connection.commits == 1 and not connection.closed
    assert candidate == before and Path(result["media"]["local_path"]).is_file()
    again = retrieve_candidate_media(
        candidate, storage=TigerDataPostStore(Connection()), output_dir=tmp_path
    )
    assert again["post_id"] != result["post_id"]
    assert again["media"]["media_id"] != result["media"]["media_id"]


def test_candidate_default_store_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: Candidate
) -> None:
    module = importlib.import_module("backend.services.external_apis.retrieve_post")
    monkeypatch.setattr(module.subprocess, "run", downloader)
    connection = Connection()
    monkeypatch.setattr(
        TigerDataPostStore,
        "from_env",
        lambda: TigerDataPostStore(connection, owned_connection=True),
    )
    result = retrieve_candidate_media(candidate, output_dir=tmp_path)
    assert result["candidate_id"] == candidate["candidate_id"] and connection.closed


def test_candidate_storage_failure_has_correct_stage_and_removes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: Candidate
) -> None:
    module = importlib.import_module("backend.services.external_apis.retrieve_post")
    monkeypatch.setattr(module.subprocess, "run", downloader)
    connection = Connection()
    connection.fail = True
    with pytest.raises(QuantroError) as caught:
        retrieve_candidate_media(
            candidate, storage=TigerDataPostStore(connection), output_dir=tmp_path
        )
    assert caught.value.to_dict()["stage"] == "retrieve_candidate_media"
    assert caught.value.to_dict()["code"] == "STORAGE_FAILED"
    assert "secret" not in str(caught.value) and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field", ["candidate_id", "url", "snippet", "discovered_by_queries"]
)
def test_candidate_requires_complete_contract(candidate: Candidate, field: str) -> None:
    cast(Any, candidate).pop(field)
    with pytest.raises(QuantroError) as caught:
        retrieve_candidate_media(candidate)
    assert caught.value.to_dict()["stage"] == "retrieve_candidate_media"


def test_backend_environment_is_loaded_without_overriding_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env").write_text(
        "TIGERDATA_DSN=backend\nBACKEND_ONLY=kept\n"
    )
    (tmp_path / ".env").write_text("TIGERDATA_DSN=root\n")
    module = importlib.import_module("backend.services.external_apis.tigerdata_storage")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("TIGERDATA_DSN", raising=False)
    monkeypatch.delenv("BACKEND_ONLY", raising=False)
    values = module._environment()
    assert values["TIGERDATA_DSN"] == "root" and values["BACKEND_ONLY"] == "kept"


def test_three_stage_handoff_preserves_contract_and_stored_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    post: Post,
    features: dict[str, Any],
) -> None:
    queries = build_search_queries(features, post)
    results = search_candidates(
        queries, 5, provider=Provider([(post, queries["queries"])])
    )
    module = importlib.import_module("backend.services.external_apis.retrieve_post")
    monkeypatch.setattr(module.subprocess, "run", downloader)
    connection = Connection()
    candidate = results["candidates"][0]
    source = retrieve_candidate_media(
        candidate, storage=TigerDataPostStore(connection), output_dir=tmp_path
    )
    assert set(source) == set(post)
    assert set(source["media"]) == set(post["media"])
    assert source["candidate_id"] == candidate["candidate_id"]
    assert source["post_id"] != post["post_id"]
    assert source["media"]["media_id"] != post["media"]["media_id"]
    encoded = connection.calls[0][1][6]
    assert isinstance(encoded, str) and json.loads(encoded) == source


def test_large_tokens_cannot_break_generated_query_limit(
    features: dict[str, Any], post: Post
) -> None:
    post["title"] = " ".join(["word" * 20] * 12)
    features["transcript"]["sentences"] = [{"text": "x" * 4000}]
    result = build_search_queries(features, post)
    assert all(len(query) <= 512 for query in result["queries"])
    search_candidates(result, 5, provider=Provider([]))


def test_search_normalizes_known_dates_to_utc(post: Post) -> None:
    post["published_at"] = "2026-10-10T12:00:00-04:00"
    result = search_candidates([QUERY], 5, provider=Provider([(post, [QUERY])]))
    assert result["candidates"][0]["published_at"] == "2026-10-10T16:00:00Z"


def test_candidate_download_failure_retains_stage_and_retryability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: Candidate
) -> None:
    module = importlib.import_module("backend.services.external_apis.retrieve_post")

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("worker", 1)

    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(QuantroError) as caught:
        retrieve_candidate_media(candidate, output_dir=tmp_path)
    assert caught.value.to_dict()["stage"] == "retrieve_candidate_media"
    assert caught.value.retryable and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("inside_backend", [False, True])
def test_modules_import_in_both_repository_launch_modes(inside_backend: bool) -> None:
    root = Path(__file__).resolve().parents[1]
    prefix = "services" if inside_backend else "backend.services"
    commands = "; ".join(
        f"from {prefix}.external_apis.{name} import {name}"
        for name in (
            "build_search_queries",
            "search_candidates",
            "retrieve_candidate_media",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", commands],
        cwd=root / "backend" if inside_backend else root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_bounded_provider_prefix_includes_ocr_and_title(features, post):
    features["transcript"]["sentences"] = [{"text": f"Distinctive spoken passage number {n} about bridges"} for n in range(5)]
    features["visual_text"] = [{"text": "City Council Meeting"}]
    queries = build_search_queries(features, post)["queries"]
    assert '"City Council Meeting"' in queries[:4]
    assert '"Example Interview"' in queries[:4]
    assert sum("Distinctive spoken" in query for query in queries[:4]) == 2
