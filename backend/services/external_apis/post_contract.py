"""JSON-native contracts matching HackNC (4), version 2, pages 3-5.

Paths refer to files on the retrieval worker's filesystem, not browser URLs.
"""

from typing import TypedDict


class Author(TypedDict):
    id: str | None
    handle: str | None
    display_name: str | None
    profile_url: str | None
    bio: str | None
    verified: bool | None
    followers_count: int | None
    following_count: int | None
    posts_count: int | None


class Engagement(TypedDict):
    views_count: int | None
    likes_count: int | None
    comments_count: int | None
    shares_count: int | None


class MediaAsset(TypedDict):
    media_id: str
    local_path: str
    mime_type: str
    playback_url: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool | None


class Retrieval(TypedDict):
    retrieved_at: str
    provider: str
    metadata_source_urls: list[str]
    unavailable_fields: list[str]
    warnings: list[str]


class Post(TypedDict):
    post_id: str
    candidate_id: str | None
    post_url: str
    platform: str
    title: str | None
    caption: str | None
    author: Author
    engagement: Engagement
    published_at: str | None
    media: MediaAsset
    retrieval: Retrieval
