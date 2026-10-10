"""Atomically persist downloaded media bytes and the unchanged Post in Tiger Data.

Apply tigerdata_schema.sql before use. Runtime performs one bound INSERT and no
DDL. Injected connections remain caller-owned and must have autocommit disabled.
"""

from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Protocol, cast

from .post_contract import Post

ROOT = Path(__file__).resolve().parents[3]
MAX_MEDIA_BYTES = 100_000_000
logger = logging.getLogger(__name__)
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.?\Z")
_INSERT = (
    "INSERT INTO quantro_retrieval.posts "
    "(post_id, media_id, mime_type, sha256, size_bytes, media_bytes, metadata) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING post_id"
)
_VECTOR = """to_tsvector('simple',
    coalesce(p.metadata->>'title', '') || ' ' ||
    coalesce(p.metadata->>'caption', '') || ' ' ||
    coalesce(p.metadata #>> '{author,display_name}', '') || ' ' ||
    coalesce(p.metadata #>> '{author,handle}', ''))"""
_SEARCH = f"""
WITH inputs AS (SELECT unnest(%s::text[]) AS query),
matched AS MATERIALIZED (
    SELECT p.post_id, p.metadata, i.query,
           ts_rank_cd({_VECTOR}, websearch_to_tsquery('simple', i.query)) AS score
    FROM quantro_retrieval.posts p CROSS JOIN inputs i
    WHERE {_VECTOR} @@ websearch_to_tsquery('simple', i.query)
), grouped AS (
    SELECT metadata->>'post_url' AS url,
           array_agg(DISTINCT query ORDER BY query) AS discovered,
           max(score) AS score
    FROM matched GROUP BY metadata->>'post_url'
), representative AS (
    SELECT DISTINCT ON (metadata->>'post_url')
           metadata->>'post_url' AS url, metadata
    FROM matched ORDER BY metadata->>'post_url', post_id DESC
)
SELECT r.metadata, g.discovered
FROM grouped g JOIN representative r USING (url)
ORDER BY g.score DESC, g.url LIMIT %s
"""


class StorageError(Exception):
    """Safe error which never exposes connection strings or database diagnostics."""

    code = "STORAGE_FAILED"


class Cursor(Protocol):
    def execute(self, command: str, parameters: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def close(self) -> None: ...


class Connection(Protocol):
    autocommit: bool

    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class SearchCursor(Cursor, Protocol):
    def fetchall(self) -> list[tuple[object, object]]: ...


def _environment() -> dict[str, str]:
    """Read local settings without changing the process's environment or other keys."""
    try:
        dotenv = importlib.import_module("dotenv")
        values: dict[str, str] = {}
        for filename in ("backend/.env", ".env", ".env.local"):
            parsed = dotenv.dotenv_values(ROOT / filename, interpolate=False)
            values.update(
                (key, value)
                for key, value in parsed.items()
                if isinstance(key, str) and isinstance(value, str)
            )
        values.update(os.environ)
        return values
    except (ImportError, OSError):
        raise StorageError("Could not read Tiger Data environment settings.") from None


def _validate_dsn(parameters: dict[str, str], values: dict[str, str]) -> None:
    host = parameters.get("host", "")
    if (
        "service" in parameters
        or "hostaddr" in parameters
        or values.get("PGSERVICE")
        or values.get("PGHOSTADDR")
        or not host
        or "," in host
        or any(character.isspace() for character in host)
    ):
        raise StorageError(
            "Tiger Data requires one explicit TCP host without service or hostaddr settings."
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host) or ".." in host:
            raise StorageError("Tiger Data requires one explicit TCP host.") from None
    port = parameters.get("port", "5432")
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise StorageError("Tiger Data requires one valid TCP port.")


class TigerDataPostStore:
    """Store one Post and its JPG/MP4 bytes in a single PostgreSQL transaction."""

    def __init__(
        self, connection: Connection, *, owned_connection: bool = False
    ) -> None:
        if connection.autocommit:
            raise StorageError("Tiger Data storage requires autocommit disabled.")
        self._connection: Connection | None = connection
        self._owns_connection = owned_connection

    @classmethod
    def from_env(cls) -> TigerDataPostStore:
        values = _environment()
        # A present but blank primary setting is an error, never a fallback to a
        # different database the user did not choose for this storage feature.
        dsn = values.get("TIGERDATA_DSN", values.get("DATABASE_URL", ""))
        if not dsn.strip():
            raise StorageError("Set TIGERDATA_DSN before retrieving posts.")
        try:
            conninfo = importlib.import_module("psycopg.conninfo")
            parameters = cast(dict[str, str], conninfo.conninfo_to_dict(dsn))
            _validate_dsn(parameters, values)
            connector = importlib.import_module("psycopg")
            connection = cast(
                Connection,
                connector.connect(
                    dsn,
                    sslmode="verify-full",
                    sslrootcert=values.get("TIGERDATA_SSL_ROOT_CERT", "").strip()
                    or "system",
                    gssencmode="disable",
                    connect_timeout=10,
                    options="-c statement_timeout=120000",
                    autocommit=False,
                ),
            )
        except StorageError:
            raise
        except Exception as error:  # noqa: BLE001 - redact optional driver diagnostics and connection strings
            logger.warning("Tiger Data connection failed (%s)", type(error).__name__)
            raise StorageError(
                "Could not connect to Tiger Data. Check settings, certificates and access."
            ) from None
        return cls(connection, owned_connection=True)

    def save_post(self, post: Post) -> None:
        connection = self._connection
        if connection is None:
            raise StorageError("The Tiger Data connection is closed.")
        cursor: Cursor | None = None
        try:
            media = post.get("media")
            if not isinstance(media, dict):
                raise StorageError("Post must contain a downloaded MediaAsset.")
            post_id = post.get("post_id")
            media_id = media.get("media_id")
            mime_type = media.get("mime_type")
            local_path = media.get("local_path")
            if not isinstance(post_id, str) or not post_id or len(post_id) > 255:
                raise StorageError("The post identifier is invalid.")
            if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
                raise StorageError("The media identifier is invalid.")
            if mime_type not in ("image/jpeg", "video/mp4") or not isinstance(
                local_path, str
            ):
                raise StorageError(
                    "Tiger Data storage accepts downloaded JPG and MP4 files."
                )
            path = Path(local_path)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise StorageError("The downloaded media file is unavailable.")
            metadata = json.dumps(
                post, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            if json.loads(metadata) != post:
                raise StorageError(
                    "The Post must contain unchanged JSON-native metadata."
                )
            with path.open("rb") as stream:
                attributes = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(attributes.st_mode)
                    or path.is_symlink()
                    or not 0 < attributes.st_size <= MAX_MEDIA_BYTES
                ):
                    raise StorageError(
                        "Media must be a nonempty regular file of at most 100 MB."
                    )
                content = stream.read(MAX_MEDIA_BYTES + 1)
                if len(content) != attributes.st_size or len(content) > MAX_MEDIA_BYTES:
                    raise StorageError(
                        "The downloaded media changed or exceeds the storage limit."
                    )
            if (
                mime_type == "image/jpeg" and not content.startswith(b"\xff\xd8\xff")
            ) or (
                mime_type == "video/mp4"
                and (len(content) < 12 or content[4:8] != b"ftyp")
            ):
                raise StorageError(
                    "The downloaded media does not match its declared type."
                )
            cursor = connection.cursor()
            cursor.execute(
                _INSERT,
                (
                    post_id,
                    media_id,
                    mime_type,
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    content,
                    metadata,
                ),
            )
            returned = cursor.fetchone()
            if returned is None or len(returned) != 1 or returned[0] != post_id:
                raise StorageError("Tiger Data did not confirm the saved post.")
            connection.commit()
        except Exception as error:
            try:
                connection.rollback()
            except Exception as rollback_error:  # noqa: BLE001 - cleanup must preserve the safe original failure
                logger.warning(
                    "Tiger Data rollback failed (%s)", type(rollback_error).__name__
                )
            if isinstance(error, StorageError):
                raise
            raise StorageError(
                "Tiger Data could not persist the media and post."
            ) from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_error:  # noqa: BLE001 - preserve the committed result and redact diagnostics
                    logger.warning(
                        "Tiger Data cursor close failed (%s)",
                        type(close_error).__name__,
                    )

    def find_posts(self, queries: list[str], limit: int) -> list[tuple[object, object]]:
        """Search existing Post metadata with bound queries; never reads media bytes.

        Use a dedicated idle connection. Both success and failure end the SELECT
        transaction by rollback; this method never changes corpus records.
        """
        connection = self._connection
        if connection is None:
            raise StorageError("The Tiger Data connection is closed.")
        cursor: SearchCursor | None = None
        try:
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 50
                or not isinstance(queries, list)
                or not 1 <= len(queries) <= 20
                or any(
                    not isinstance(q, str) or not q.strip() or len(q) > 512
                    for q in queries
                )
            ):
                raise StorageError("Search parameters are invalid.")
            cursor = cast(SearchCursor, connection.cursor())
            cursor.execute(_SEARCH, (queries, limit))
            rows = cursor.fetchall()
            connection.rollback()
            return rows
        except Exception:  # noqa: BLE001 - database diagnostics can expose credentials
            try:
                connection.rollback()
            except Exception as error:  # noqa: BLE001 - preserve the safe failure
                logger.warning(
                    "Tiger Data search rollback failed (%s)", type(error).__name__
                )
            raise StorageError("Tiger Data source search failed.") from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as error:  # noqa: BLE001 - preserve result and redact diagnostics
                    logger.warning(
                        "Tiger Data search cursor close failed (%s)",
                        type(error).__name__,
                    )

    def close(self) -> None:
        """Release only a connection opened or explicitly owned by this store."""
        if self._owns_connection and self._connection is not None:
            connection = self._connection
            self._connection = None
            self._owns_connection = False
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - optional driver errors may contain credentials
                raise StorageError(
                    "Could not close the Tiger Data connection."
                ) from None
