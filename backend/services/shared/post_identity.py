"""Post identity checks shared by source selection and report boundaries."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import ErrorCode, QuantroError, Stage
from .types import PublicPost


def normalized_post_url(value: str) -> str:
    """Normalize a URL without discarding query values that may identify media."""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        port = url.port
        if port is not None and (url.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{port}"
        return urlunsplit((url.scheme.lower(), host, url.path.rstrip("/") or "/", urlencode(sorted(parse_qsl(url.query))), ""))
    except (TypeError, ValueError):
        return value


def post_identity(value: str) -> str:
    """Identify known platform posts independently of share links and tracking."""
    normalized = normalized_post_url(value)
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (url.scheme not in {"http", "https"} or url.username is not None or url.password is not None
                or (url.port is not None and (url.scheme, url.port) not in {("http", 80), ("https", 443)})):
            return normalized
        if host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            match = re.fullmatch(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)/?", url.path)
            if match:
                return f"instagram:{match[1]}"
        if host in {"youtu.be", "www.youtu.be"}:
            match = re.fullmatch(r"/([A-Za-z0-9_-]+)/?", url.path)
            if match:
                return f"youtube:{match[1]}"
        if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            match = re.fullmatch(r"/@[^/]+/video/(\d+)/?", url.path)
            if match:
                return f"tiktok:{match[1]}"
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if url.path.rstrip("/") == "/watch":
                videos = [item for key, item in parse_qsl(url.query) if key == "v"]
                if len(videos) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", videos[0]):
                    return f"youtube:{videos[0]}"
            match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)/?", url.path)
            if match:
                return f"youtube:{match[1]}"
    except (TypeError, ValueError):
        pass
    return normalized


def post_identities(post: PublicPost) -> set[str]:
    identities = {post_identity(post.post_url)}
    # Retrieval retains the original share URL in post_url; its provider-resolved
    # webpage URL is provenance. Only concrete platform post IDs count here.
    for source in post.retrieval.metadata_source_urls:
        identity = post_identity(source)
        if identity.startswith(("instagram:", "youtube:", "tiktok:")) and source.lower().startswith(("http://", "https://")):
            identities.add(identity)
    return identities


def ensure_distinct_source(submitted: PublicPost, source: PublicPost | None, stage: Stage) -> None:
    """Reject self-matches without changing saved evidence or report contracts."""
    if source is not None and post_identities(submitted) & post_identities(source):
        raise QuantroError(
            ErrorCode.PROCESSING_FAILED,
            stage,
            "The submitted post cannot be its own source. Run a new analysis job.",
        )
