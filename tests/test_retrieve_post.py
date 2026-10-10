"""Post retrieval contract, downloaded-file handoff and failure cleanup (no network)."""

import base64
import json
import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.services.external_apis import retrieve_post as retrieval
from backend.services.external_apis.post_contract import MediaAsset, Post

RetrievalError = retrieval.RetrievalError
retrieve_post = retrieval.retrieve_post

POST_URL = "https://www.youtube.com/watch?v=abcdefghijk"
CAPTION = "A real caption: café, 世界.\nSecond line."


EMPTY_AUTHOR = {
    "id": None,
    "handle": None,
    "display_name": None,
    "profile_url": None,
    "bio": None,
    "verified": None,
    "followers_count": None,
    "following_count": None,
    "posts_count": None,
}
EMPTY_ENGAGEMENT = {
    "views_count": None,
    "likes_count": None,
    "comments_count": None,
    "shares_count": None,
}


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body) + 8) + kind + body


# Small ISO-BMFF fixture. Retrieval validates the container, while decoding belongs to extraction.
MP4 = (
    box(b"ftyp", b"isom\x00\x00\x02\x00isommp41")
    + box(b"mdat", b"\x00" * 64)
    + box(b"moov", b"")
)


class SaveStore:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.posts: list[Post] = []
        self.media_bytes: list[bytes] = []
        self.failure = failure
        self.closed = False

    def save_post(self, post: Post) -> None:
        self.posts.append(post)
        media = post["media"]
        self.media_bytes.append(Path(media["local_path"]).read_bytes())
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def default_store(monkeypatch: pytest.MonkeyPatch) -> SaveStore:
    store = SaveStore()
    monkeypatch.setattr(
        "backend.services.external_apis.retrieve_post.SnowflakePostStore.from_env",
        lambda: store,
    )
    return store


class Downloader:
    """The worker's subprocess boundary, including realistic output files and metadata."""

    def __init__(
        self,
        *,
        metadata: dict[str, object] | None = None,
        content: bytes = MP4,
        write_file: bool = True,
        partial: bool = False,
        timeout: bool = False,
        returncode: int = 0,
        stdout: str | None = None,
        stderr: str = "",
    ) -> None:
        self.metadata: dict[str, object] = {
            "ext": "mp4",
            "protocol": "https",
            "vcodec": "avc1",
            "acodec": "mp4a",
            **(
                {
                    "id": "abcdefghijk",
                    "extractor_key": "Youtube",
                    "webpage_url": POST_URL,
                    "description": CAPTION,
                    "uploader_id": "@example",
                    "uploader": "Example Creator",
                    "timestamp": 1_700_000_000,
                }
                if metadata is None
                else metadata
            ),
        }
        self.content = content
        self.write_file = write_file
        self.partial = partial
        self.timeout = timeout
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.paths: list[Path] = []

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), kwargs))
        image = "--direct-image" in args
        if image:
            path = Path(args[args.index("--output-file") + 1])
        else:
            option = "--output" if "--output" in args else "-o"
            template = args[args.index(option) + 1]
            path = Path(template).parent / "media.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.append(path)
        if self.write_file:
            target = path.with_suffix(path.suffix + ".part") if self.partial else path
            target.write_bytes(self.content)
        if self.timeout:
            timeout = kwargs.get("timeout", 120)
            assert isinstance(timeout, (float, int))
            raise subprocess.TimeoutExpired(args, timeout)
        metadata = {
            **self.metadata,
            "_filename": str(path),
            "filepath": str(path),
            "requested_downloads": [{"filepath": str(path), "ext": "mp4"}],
        }
        if image:
            metadata.update({"_type": "image", "ext": "jpg"})
        stdout = json.dumps(metadata) if self.stdout is None else self.stdout
        return subprocess.CompletedProcess(args, self.returncode, stdout, self.stderr)


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata: dict[str, object] | None = None,
    content: bytes = MP4,
    write_file: bool = True,
    partial: bool = False,
    timeout: bool = False,
    returncode: int = 0,
    stdout: str | None = None,
    stderr: str = "",
) -> Downloader:
    downloader = Downloader(
        metadata=metadata,
        content=content,
        write_file=write_file,
        partial=partial,
        timeout=timeout,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    monkeypatch.setattr(
        "backend.services.external_apis.retrieve_post.subprocess.run", downloader
    )
    return downloader


def assert_clean(directory: Path) -> None:
    assert not list(directory.rglob("*")), (
        "Failed retrieval must remove its files and private working directory"
    )


def test_native_json_contract_can_be_passed_directly_to_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert set(post) == {
        "post_id",
        "candidate_id",
        "post_url",
        "platform",
        "title",
        "caption",
        "author",
        "published_at",
        "engagement",
        "media",
        "retrieval",
    }
    assert post["post_id"]
    assert post["post_url"] == POST_URL
    assert post["platform"] == "youtube"
    assert post["caption"] == CAPTION
    assert post["author"] == {
        **EMPTY_AUTHOR,
        "id": "@example",
        "handle": "@example",
        "display_name": "Example Creator",
    }
    assert post["published_at"] is not None
    assert datetime.fromisoformat(post["published_at"]) == datetime.fromtimestamp(
        1_700_000_000, UTC
    )
    media = post["media"]
    assert set(media) == {
        "media_id",
        "local_path",
        "mime_type",
        "playback_url",
        "duration_seconds",
        "width",
        "height",
        "fps",
        "has_audio",
    }
    assert media["media_id"]
    assert media["mime_type"] == "video/mp4"
    assert media["playback_url"] is None
    local_path = Path(media["local_path"])
    assert local_path.is_absolute()
    assert local_path.is_relative_to(tmp_path)
    assert local_path.read_bytes() == MP4
    assert json.loads(json.dumps(post)) == post

    def extract_media_features(asset: MediaAsset) -> bytes:
        return Path(asset["local_path"]).read_bytes()

    assert extract_media_features(post["media"]) == MP4


@pytest.mark.parametrize(
    "metadata",
    [
        {"id": "abcdefghijk", "ext": "mp4"},
        {
            "id": "abcdefghijk",
            "ext": "mp4",
            "title": "A title is not the submitted caption",
        },
        {
            "id": "abcdefghijk",
            "ext": "mp4",
            "description": None,
            "upload_date": "20261010",
        },
    ],
)
def test_missing_metadata_remains_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata: dict[str, object]
) -> None:
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["caption"] is None
    assert post["author"] == EMPTY_AUTHOR
    assert post["published_at"] is None
    assert Path(post["media"]["local_path"]).is_file()


def test_author_may_have_only_display_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(
        monkeypatch,
        metadata={"id": "abcdefghijk", "ext": "mp4", "uploader": "Only a display name"},
    )

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"] == {**EMPTY_AUTHOR, "display_name": "Only a display name"}


def test_submitted_url_is_preserved_and_each_call_owns_distinct_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)
    submitted = POST_URL + "&t=12"

    first = retrieve_post(submitted, output_dir=tmp_path)
    second = retrieve_post(submitted, output_dir=tmp_path)

    assert first["post_url"] == second["post_url"] == submitted
    assert first["post_id"] != second["post_id"]
    assert first["media"]["media_id"] != second["media"]["media_id"]
    assert first["media"]["local_path"] != second["media"]["local_path"]
    assert Path(first["media"]["local_path"]).read_bytes() == MP4


@pytest.mark.parametrize(
    "url",
    [
        "",
        "just some text",
        "file:///etc/passwd",
        "ftp://www.youtube.com/watch?v=abcdefghijk",
        "http://127.0.0.1/private.mp4",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/private.mp4",
        "https://user:password@www.youtube.com/watch?v=abcdefghijk",
        "https://www.youtube.com.evil.example/watch?v=abcdefghijk",
        "https://example.com/post/1",
    ],
)
def test_invalid_or_unsupported_urls_fail_before_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    downloader = install(monkeypatch)

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(url, output_dir=tmp_path)

    assert caught.value.stage == "retrieve_post"
    assert caught.value.retryable is False
    assert downloader.calls == []
    assert_clean(tmp_path)


def test_subprocess_has_finite_deadline_and_does_not_use_a_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = install(monkeypatch)

    retrieve_post(POST_URL, output_dir=tmp_path, timeout_seconds=7)

    args, options = downloader.calls[0]
    assert options["timeout"] == 7
    assert options.get("shell") is not True
    assert "--no-playlist" in args
    assert "--ignore-config" in args
    assert "--proxy" in args and args[args.index("--proxy") + 1] == ""


def test_timeout_removes_partial_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, partial=True, timeout=True)

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path, timeout_seconds=1)

    assert caught.value.retryable is True
    assert_clean(tmp_path)


def test_oversized_download_is_rejected_and_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path, max_bytes=len(MP4) - 1)

    assert caught.value.retryable is False
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "content", [b"", b"<!doctype html><html>Sign in</html>", b"not a video"]
)
def test_empty_html_and_non_media_downloads_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> None:
    install(monkeypatch, content=content)

    with pytest.raises(RetrievalError):
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        box(b"ftyp", b"isom"),
        struct.pack(">I", 1024) + b"ftypisom",
        box(b"ftyp", b"isom") + box(b"moov", b""),
        box(b"ftyp", b"isom") + box(b"mdat", b"\x00" * 8),
        MP4 + b"\x00",
        box(b"ftyp", b"isom") + struct.pack(">I", 1) + b"mdat" + b"\x00" * 4,
    ],
    ids=[
        "header-only",
        "truncated-box",
        "missing-mdat",
        "missing-moov",
        "trailing-partial-box",
        "short-extended-box",
    ],
)
def test_incomplete_mp4_containers_are_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> None:
    install(monkeypatch, content=content)

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert caught.value.retryable is False
    assert_clean(tmp_path)


@pytest.mark.parametrize("partial", [False, True])
def test_missing_or_unfinished_download_cannot_return_a_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, partial: bool
) -> None:
    install(monkeypatch, write_file=partial, partial=partial)

    with pytest.raises(RetrievalError):
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "metadata",
    [
        {"_type": "playlist", "entries": [{"id": "one"}, {"id": "two"}]},
        {"_type": "multi_video", "entries": [{"id": "one"}]},
        {"id": "abcdefghijk", "is_live": True},
        {"id": "abcdefghijk", "live_status": "is_live"},
    ],
)
def test_playlists_multi_video_and_live_content_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata: dict[str, object]
) -> None:
    install(monkeypatch, metadata=metadata)

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert caught.value.retryable is False
    assert_clean(tmp_path)


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "null", '{"id":'])
def test_malformed_worker_output_is_a_typed_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    install(monkeypatch, stdout=stdout)

    with pytest.raises(RetrievalError):
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert_clean(tmp_path)


def test_provider_failure_is_safe_json_and_does_not_echo_sensitive_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "private-query-token"
    submitted = POST_URL + "&token=" + secret
    install(
        monkeypatch,
        returncode=1,
        stderr=f"HTTP 403 for {submitted} Cookie: secret-session",
    )

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(submitted, output_dir=tmp_path)

    error = caught.value
    serialized = json.dumps(error.as_dict())
    assert set(error.as_dict()) == {"error"}
    assert set(error.as_dict()["error"]) == {"code", "stage", "message", "retryable"}
    assert error.code
    assert error.stage == "retrieve_post"
    assert isinstance(error.retryable, bool)
    assert secret not in serialized
    assert "secret-session" not in serialized
    assert submitted not in serialized
    assert_clean(tmp_path)


def test_direct_public_mp4_has_the_same_downstream_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, metadata={"id": "clip", "extractor_key": "Generic"})
    url = "https://cdn.example.com/clip.mp4?version=2"

    post = retrieve_post(url, output_dir=tmp_path)

    assert post["post_url"] == url
    assert post["platform"] == "direct"
    assert post["caption"] is None
    assert post["author"] == EMPTY_AUTHOR
    assert post["published_at"] is None
    assert Path(post["media"]["local_path"]).read_bytes() == MP4
    assert json.loads(json.dumps(post)) == post


@pytest.mark.parametrize(
    "timeout_seconds",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, False, 10**1000],
    ids=["zero", "negative", "nan", "inf", "negative-inf", "true", "false", "huge-int"],
)
def test_invalid_timeout_limits_fail_without_subprocess_or_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timeout_seconds: float
) -> None:
    downloader = install(monkeypatch)

    with pytest.raises(ValueError):
        retrieve_post(POST_URL, output_dir=tmp_path, timeout_seconds=timeout_seconds)

    assert downloader.calls == []
    assert_clean(tmp_path)


@pytest.mark.parametrize("max_bytes", [0, -1, True, False])
def test_invalid_size_limits_fail_without_subprocess_or_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, max_bytes: int
) -> None:
    downloader = install(monkeypatch)

    with pytest.raises(ValueError):
        retrieve_post(POST_URL, output_dir=tmp_path, max_bytes=max_bytes)

    assert downloader.calls == []
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        True,
        False,
        "20261010",
        "1700000000",
        float("nan"),
        float("inf"),
        float("-inf"),
        1e200,
        10**1000,
    ],
    ids=[
        "missing",
        "true",
        "false",
        "date-string",
        "numeric-string",
        "nan",
        "inf",
        "negative-inf",
        "range",
        "huge-int",
    ],
)
def test_invalid_publication_timestamps_remain_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, timestamp: object
) -> None:
    install(
        monkeypatch,
        metadata={
            "id": "abcdefghijk",
            "timestamp": timestamp,
            "upload_date": "20261010",
        },
    )

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["published_at"] is None
    assert Path(post["media"]["local_path"]).read_bytes() == MP4


JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsL"
    "EBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAw"
    "QFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFh"
    "cYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpK"
    "Wmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQ"
    "AAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYn"
    "LRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaX"
    "mJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPw"
    "D8qqKKKAP/2Q=="
)


def test_success_is_persisted_once_before_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(monkeypatch)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert default_store.posts == [post]
    assert default_store.media_bytes == [MP4]
    assert default_store.closed


def test_injected_store_receives_exact_downstream_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(monkeypatch)
    supplied_store = SaveStore()

    post = retrieve_post(POST_URL, output_dir=tmp_path, storage=supplied_store)

    assert supplied_store.posts == [post]
    assert supplied_store.posts[0] is post
    assert supplied_store.media_bytes == [
        Path(post["media"]["local_path"]).read_bytes()
    ]
    assert default_store.posts == []


def test_storage_failure_returns_safe_error_and_removes_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)
    secret = "private-snowflake-token"
    store = SaveStore(failure=RuntimeError(secret))

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path, storage=store)

    assert caught.value.code == "STORAGE_FAILED"
    assert caught.value.stage == "retrieve_post"
    assert secret not in str(caught.value)
    assert secret not in json.dumps(caught.value.as_dict())
    assert len(store.posts) == 1
    assert_clean(tmp_path)


def test_invalid_download_never_reaches_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(monkeypatch, content=b"This is an error page.")

    with pytest.raises(RetrievalError):
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert default_store.posts == []
    assert_clean(tmp_path)


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "JPG"])
def test_direct_jpeg_download_is_persisted_and_ready_for_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_store: SaveStore,
    extension: str,
) -> None:
    downloader = install(monkeypatch, metadata={}, content=JPEG)
    url = f"https://cdn.example.com/photo.{extension}?v=1"

    post = retrieve_post(url, output_dir=tmp_path)

    assert post["post_url"] == url
    assert post["platform"] == "direct"
    assert post["caption"] is None
    assert post["author"] == EMPTY_AUTHOR
    assert post["published_at"] is None
    assert post["media"]["mime_type"] == "image/jpeg"
    path = Path(post["media"]["local_path"])
    assert path.suffix == ".jpg"
    assert path.read_bytes() == JPEG
    assert default_store.posts == [post]
    assert default_store.media_bytes == [JPEG]
    args, options = downloader.calls[0]
    assert args[args.index("--direct-image") + 1] == url
    assert args[args.index("--output-file") + 1] == str(path)
    assert "--max-bytes" in args
    assert options.get("shell") is not True


@pytest.mark.parametrize("content", [b"", b"<!doctype html>not found", JPEG[:-2], MP4])
def test_non_jpeg_image_download_fails_before_storage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_store: SaveStore,
    content: bytes,
) -> None:
    install(monkeypatch, metadata={}, content=content)

    with pytest.raises(RetrievalError):
        retrieve_post("https://cdn.example.com/photo.jpg", output_dir=tmp_path)

    assert default_store.posts == []
    assert_clean(tmp_path)


def test_nonexistent_post_errors_without_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(
        monkeypatch, returncode=1, write_file=False, stderr="HTTP Error 404: Not Found"
    )

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert caught.value.stage == "retrieve_post"
    assert set(caught.value.as_dict()["error"]) == {
        "code",
        "stage",
        "message",
        "retryable",
    }
    assert default_store.posts == []
    assert_clean(tmp_path)


def complete_metadata() -> dict[str, object]:
    return {
        "id": "abcdefghijk",
        "title": "A published title",
        "description": CAPTION,
        "timestamp": 1_700_000_000,
        "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk&canonical=1",
        "channel_id": "channel-123",
        "uploader_id": "@example",
        "uploader": "Example Creator",
        "uploader_url": "https://www.youtube.com/@example",
        "uploader_description": "Creator biography",
        "channel_is_verified": True,
        "channel_follower_count": 1200,
        "uploader_following_count": 15,
        "uploader_video_count": 28,
        "view_count": 5400,
        "like_count": 250,
        "comment_count": 18,
        "repost_count": 9,
        "duration": 12.5,
        "width": 1920,
        "height": 1080,
        "fps": 29.97,
        "acodec": "mp4a",
    }


def test_enriched_contract_maps_available_provider_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    metadata = complete_metadata()
    install(monkeypatch, metadata=metadata)
    before = datetime.now(UTC)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    after = datetime.now(UTC)
    assert post["candidate_id"] is None
    assert post["title"] == "A published title"
    assert post["caption"] == CAPTION
    assert post["author"] == {
        "id": "channel-123",
        "handle": "@example",
        "display_name": "Example Creator",
        "profile_url": "https://www.youtube.com/@example",
        "bio": "Creator biography",
        "verified": True,
        "followers_count": 1200,
        "following_count": 15,
        "posts_count": 28,
    }
    assert post["engagement"] == {
        "views_count": 5400,
        "likes_count": 250,
        "comments_count": 18,
        "shares_count": 9,
    }
    assert post["media"]["duration_seconds"] == 12.5
    assert post["media"]["width"] == 1920
    assert post["media"]["height"] == 1080
    assert post["media"]["fps"] == 29.97
    assert post["media"]["has_audio"] is True
    provenance = post["retrieval"]
    assert set(provenance) == {
        "retrieved_at",
        "provider",
        "metadata_source_urls",
        "unavailable_fields",
        "warnings",
    }
    assert provenance["retrieved_at"].endswith("Z")
    retrieved = datetime.fromisoformat(provenance["retrieved_at"])
    assert before <= retrieved <= after
    assert provenance["provider"] == "yt-dlp"
    assert provenance["metadata_source_urls"] == [
        POST_URL,
        metadata["webpage_url"],
    ]
    assert provenance["unavailable_fields"] == []
    assert provenance["warnings"] == []
    assert json.loads(json.dumps(post, allow_nan=False)) == post
    assert default_store.posts == [post]


def test_missing_optional_metadata_has_complete_nullable_objects_and_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, metadata={"id": "abcdefghijk"})

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["candidate_id"] is None
    assert post["title"] is None
    assert post["caption"] is None
    assert post["published_at"] is None
    assert post["author"] == EMPTY_AUTHOR
    assert post["engagement"] == EMPTY_ENGAGEMENT
    missing = set(post["retrieval"]["unavailable_fields"])
    expected = {
        "title",
        "caption",
        "published_at",
        *(f"author.{field}" for field in EMPTY_AUTHOR),
        *(f"engagement.{field}" for field in EMPTY_ENGAGEMENT),
        "media.duration_seconds",
        "media.width",
        "media.height",
        "media.fps",
    }
    assert missing == expected
    assert post["retrieval"]["metadata_source_urls"] == [POST_URL]
    assert post["retrieval"]["warnings"]
    assert json.loads(json.dumps(post, allow_nan=False)) == post


def test_zero_counts_and_false_verification_are_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metadata = complete_metadata()
    for name in (
        "channel_follower_count",
        "uploader_following_count",
        "uploader_video_count",
        "view_count",
        "like_count",
        "comment_count",
        "repost_count",
        "width",
        "height",
        "duration",
        "fps",
    ):
        metadata[name] = 0
    metadata["channel_is_verified"] = False
    metadata["acodec"] = "none"
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"]["verified"] is False
    assert post["author"]["followers_count"] == 0
    assert post["author"]["following_count"] == 0
    assert post["author"]["posts_count"] == 0
    assert post["engagement"] == dict.fromkeys(EMPTY_ENGAGEMENT, 0)
    assert post["media"]["width"] is None
    assert post["media"]["height"] is None
    assert post["media"]["duration_seconds"] == 0
    assert post["media"]["fps"] is None
    assert post["media"]["has_audio"] is False
    assert set(post["retrieval"]["unavailable_fields"]) == {
        "media.width",
        "media.height",
        "media.fps",
    }


@pytest.mark.parametrize(
    "bad", [True, False, -1, 1.5, float("nan"), float("inf"), "20", {}, []]
)
def test_bad_counts_and_dimensions_become_null_without_blocking_retrieval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: object
) -> None:
    metadata = complete_metadata()
    for name in (
        "channel_follower_count",
        "uploader_following_count",
        "uploader_video_count",
        "view_count",
        "like_count",
        "comment_count",
        "repost_count",
        "width",
        "height",
    ):
        metadata[name] = bad
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"]["followers_count"] is None
    assert post["author"]["following_count"] is None
    assert post["author"]["posts_count"] is None
    assert post["engagement"] == EMPTY_ENGAGEMENT
    assert post["media"]["width"] is None
    assert post["media"]["height"] is None
    assert json.loads(json.dumps(post, allow_nan=False)) == post


@pytest.mark.parametrize(
    "bad", [True, False, -1, float("nan"), float("inf"), "12.5", {}, []]
)
def test_bad_duration_fps_and_verification_become_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: object
) -> None:
    metadata = complete_metadata()
    metadata["duration"] = bad
    metadata["fps"] = bad
    metadata["channel_is_verified"] = "true"
    metadata["acodec"] = "unknown"
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["media"]["duration_seconds"] is None
    assert post["media"]["fps"] is None
    assert post["media"]["has_audio"] is None
    assert post["author"]["verified"] is None
    assert json.loads(json.dumps(post, allow_nan=False)) == post


def test_provider_fallback_fields_map_without_inventing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(
        monkeypatch,
        metadata={
            "id": "abcdefghijk",
            "channel_id": "channel-123",
            "channel": "Channel name",
            "channel_url": "https://www.youtube.com/channel/channel-123",
            "channel_description": "Channel biography",
            "uploader_verified": False,
            "uploader_follower_count": 0,
            "channel_video_count": 0,
            "share_count": 0,
        },
    )

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"] == {
        "id": "channel-123",
        "handle": None,
        "display_name": "Channel name",
        "profile_url": "https://www.youtube.com/channel/channel-123",
        "bio": "Channel biography",
        "verified": False,
        "followers_count": 0,
        "following_count": None,
        "posts_count": 0,
    }
    assert post["engagement"]["shares_count"] == 0


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://127.0.0.1/profile",
        "https://user:password@www.youtube.com/@example",
        "https://localhost/profile",
        "http://169.254.169.254/latest/meta-data",
        "not a URL",
        99,
        {},
    ],
)
def test_unsafe_optional_metadata_urls_are_not_exposed_as_profile_or_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_url: object
) -> None:
    metadata = complete_metadata()
    metadata["webpage_url"] = bad_url
    metadata["uploader_url"] = bad_url
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"]["profile_url"] is None
    assert post["retrieval"]["metadata_source_urls"] == [POST_URL]
    assert "author.profile_url" in post["retrieval"]["unavailable_fields"]


def test_source_urls_are_unique_and_do_not_claim_unfetched_profile_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metadata = complete_metadata()
    metadata["webpage_url"] = POST_URL
    metadata["channel_url"] = "https://www.youtube.com/channel/ignored-alternate"
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["author"]["profile_url"] == metadata["uploader_url"]
    assert post["retrieval"]["metadata_source_urls"] == [POST_URL]


def test_direct_jpeg_reports_http_provider_and_no_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch, metadata={}, content=JPEG)
    url = "https://cdn.example.com/photo.jpg"

    post = retrieve_post(url, output_dir=tmp_path)

    assert post["retrieval"]["provider"] == "direct-http"
    assert post["retrieval"]["metadata_source_urls"] == [url]
    assert post["media"]["has_audio"] is False
    assert "media.has_audio" not in post["retrieval"]["unavailable_fields"]
    assert json.loads(json.dumps(post, allow_nan=False)) == post


def test_trigger_success_returns_persisted_downstream_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(monkeypatch)

    result = retrieval.handle_post_trigger(POST_URL, output_dir=tmp_path)

    assert "error" not in result
    assert result == default_store.posts[0]
    assert Path(default_store.posts[0]["media"]["local_path"]).read_bytes() == MP4


def test_trigger_failure_returns_shared_error_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, default_store: SaveStore
) -> None:
    install(
        monkeypatch, returncode=1, write_file=False, stderr="private provider error"
    )

    result = retrieval.handle_post_trigger(POST_URL, output_dir=tmp_path)

    assert set(result) == {"error"}
    assert result == {
        "error": {
            "code": "MEDIA_UNAVAILABLE",
            "stage": "retrieve_post",
            "message": "The submitted video could not be retrieved.",
            "retryable": False,
        }
    }
    assert "private provider error" not in json.dumps(result)
    assert default_store.posts == []
    assert_clean(tmp_path)


@pytest.mark.parametrize("bad", [True, 12, {}, [], "   "])
def test_malformed_optional_text_is_null_and_never_blocks_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: object
) -> None:
    metadata = complete_metadata()
    for field in (
        "title",
        "description",
        "channel_id",
        "uploader_id",
        "uploader",
        "uploader_description",
    ):
        metadata[field] = bad
    install(monkeypatch, metadata=metadata)

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert post["title"] is None
    assert post["caption"] is None
    assert post["author"]["id"] is None
    assert post["author"]["handle"] is None
    assert post["author"]["display_name"] is None
    assert post["author"]["bio"] is None
    assert Path(post["media"]["local_path"]).read_bytes() == MP4
    assert json.loads(json.dumps(post, allow_nan=False)) == post


@pytest.mark.parametrize("kind", [[], {}])
def test_malformed_provider_content_type_is_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_store: SaveStore,
    kind: object,
) -> None:
    install(monkeypatch, metadata={"id": "abcdefghijk", "_type": kind})

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert caught.value.code == "MEDIA_UNAVAILABLE"
    assert default_store.posts == []
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    ("timeout", "expected_code"),
    [(False, "MEDIA_UNAVAILABLE"), (True, "MEDIA_TIMEOUT")],
)
def test_cleanup_failure_preserves_the_original_safe_retrieval_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    default_store: SaveStore,
    timeout: bool,
    expected_code: str,
) -> None:
    install(
        monkeypatch,
        returncode=1,
        partial=True,
        timeout=timeout,
        stderr="private provider details",
    )
    cleanup_attempts: list[Path] = []

    def denied_cleanup(path: Path, **_options: object) -> None:
        cleanup_attempts.append(path)
        raise PermissionError("private-local-path")

    monkeypatch.setattr(
        "backend.services.external_apis.retrieve_post.shutil.rmtree", denied_cleanup
    )

    with pytest.raises(RetrievalError) as caught:
        retrieve_post(POST_URL, output_dir=tmp_path)

    assert caught.value.code == expected_code
    assert caught.value.stage == "retrieve_post"
    assert len(cleanup_attempts) == 1
    assert cleanup_attempts[0].parent == tmp_path
    assert default_store.posts == []
    serialized = json.dumps(caught.value.as_dict())
    assert "private provider details" not in serialized
    assert "private-local-path" not in serialized


def test_owned_store_close_failure_preserves_the_committed_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install(monkeypatch)

    class CloseFailureStore(SaveStore):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("private-snowflake-cleanup-details")

    store = CloseFailureStore()
    monkeypatch.setattr(
        "backend.services.external_apis.retrieve_post.SnowflakePostStore.from_env",
        lambda: store,
    )

    post = retrieve_post(POST_URL, output_dir=tmp_path)

    assert store.closed
    assert store.posts == [post]
    assert store.media_bytes == [MP4]
    assert Path(post["media"]["local_path"]).read_bytes() == MP4
    assert "private-snowflake-cleanup-details" not in json.dumps(post)
