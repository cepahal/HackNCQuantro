"""Persist downloaded JPG/MP4 bytes and their unchanged pipeline Post in Snowflake.

Apply snowflake_schema.sql once before use. PUT uploads are not transactional:
an upload followed by a failed metadata insert can leave an unreferenced stage
file. No metadata row is committed until the upload result is verified.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Protocol, cast

ROOT = Path(__file__).resolve().parents[3]
MAX_MEDIA_BYTES = 100_000_000
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Safe persistence error; connector diagnostics and secrets are never exposed."""

    code = "STORAGE_FAILED"


class Cursor(Protocol):
    def execute(
        self,
        command: str,
        parameters: tuple[object, ...] | None = None,
        *,
        file_stream: BinaryIO | None = None,
        timeout: int = 120,
    ) -> object: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def _identifier(value: str) -> str:
    parts = value.split(".")
    if not 1 <= len(parts) <= 3 or any(
        not _IDENTIFIER.fullmatch(part) for part in parts
    ):
        raise StorageError(
            "Use unquoted Snowflake identifiers containing letters, digits, underscores or dollar signs."
        )
    return value


def _environment() -> dict[str, str]:
    try:
        dotenv = importlib.import_module("dotenv")
        values = dotenv.dotenv_values(ROOT / ".env")
    except (ImportError, OSError):
        raise StorageError(
            "Install backend requirements and provide Snowflake environment settings."
        ) from None
    return {
        key: value
        for key, value in {**values, **os.environ}.items()
        if isinstance(value, str)
    }


class SnowflakePostStore:
    """Synchronous store. Injected connections stay caller-owned and should disable autocommit."""

    def __init__(
        self,
        connection: Connection | None = None,
        *,
        stage: str = "QUANTRO_MEDIA",
        table: str = "QUANTRO_POSTS",
    ) -> None:
        self.stage = _identifier(stage)
        self.table = _identifier(table)
        self._connection = connection
        self._owns_connection = False

    @classmethod
    def from_env(cls) -> SnowflakePostStore:
        """Lazily import/connect using root .env plus overriding process environment."""
        values = _environment()
        required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER")
        token = values.get("SNOWFLAKE_TOKEN", "").strip()
        password = values.get("SNOWFLAKE_PASSWORD", "")
        if any(not values.get(name, "").strip() for name in required) or not (
            token or password.strip()
        ):
            raise StorageError(
                "Set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and either SNOWFLAKE_PASSWORD "
                "or SNOWFLAKE_TOKEN before retrieving posts."
            )
        # Keep existing PAT configuration authoritative; never retry with another credential.
        authentication = (
            {"authenticator": "PROGRAMMATIC_ACCESS_TOKEN", "token": token}
            if token
            else {"authenticator": "snowflake", "password": password}
        )
        store = cls(
            stage=values.get("SNOWFLAKE_MEDIA_STAGE", "QUANTRO_MEDIA"),
            table=values.get("SNOWFLAKE_POSTS_TABLE", "QUANTRO_POSTS"),
        )
        context: dict[str, str] = {}
        for name in ("WAREHOUSE", "DATABASE", "SCHEMA", "ROLE"):
            value = values.get("SNOWFLAKE_" + name, "").strip()
            if value:
                context[name.lower()] = _identifier(value)
        account = values["SNOWFLAKE_ACCOUNT"].strip()
        user = values["SNOWFLAKE_USER"].strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", account)
            or len(user) > 255
        ):
            raise StorageError("Check SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER.")
        if any(ord(character) < 32 for character in user):
            raise StorageError("Check SNOWFLAKE_USER.")
        try:
            connector = importlib.import_module("snowflake.connector")
            store._connection = cast(
                Connection,
                connector.connect(
                    account=account,
                    user=user,
                    autocommit=False,
                    login_timeout=30,
                    network_timeout=120,
                    **authentication,
                    **context,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - redact optional connector diagnostics at the persistence boundary
            logger.warning("Snowflake connection failed (%s)", type(exc).__name__)
            raise StorageError(
                "Could not connect to Snowflake. Check credentials, access and network policy."
            ) from None
        store._owns_connection = True
        return store

    def save_post(self, post: Mapping[str, object]) -> None:
        """Upload actual file bytes, then commit a bound metadata row; preserve Post fields."""
        if self._connection is None:
            configured = self.from_env()
            self._connection = configured._connection
            self._owns_connection = True
            self.stage = configured.stage
            self.table = configured.table
        connection = self._connection
        if connection is None:
            raise StorageError("Snowflake is not configured.")
        cursor: Cursor | None = None
        try:
            media = post.get("media")
            if not isinstance(media, dict):
                raise StorageError("Post must contain a downloaded MediaAsset.")
            media_id = media.get("media_id")
            post_id = post.get("post_id")
            mime_type = media.get("mime_type")
            local_path = media.get("local_path")
            if not isinstance(media_id, str) or not _MEDIA_ID.fullmatch(media_id):
                raise StorageError("The media identifier is invalid.")
            if not isinstance(post_id, str) or not post_id or len(post_id) > 255:
                raise StorageError("The post identifier is invalid.")
            if mime_type not in {"image/jpeg", "video/mp4"} or not isinstance(
                local_path, str
            ):
                raise StorageError(
                    "Snowflake media storage accepts downloaded JPG and MP4 files."
                )
            path = Path(local_path)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise StorageError("The downloaded media file is unavailable.")
            metadata = json.dumps(
                post, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            filename = "media.jpg" if mime_type == "image/jpeg" else "media.mp4"
            stage_path = f"@{self.stage}/{media_id}/{filename}"
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                if not 0 < size <= MAX_MEDIA_BYTES:
                    raise StorageError(
                        "The downloaded media must be nonempty and at most 100 MB."
                    )
                stream.seek(0)
                header = stream.read(16)
                if (
                    mime_type == "image/jpeg" and not header.startswith(b"\xff\xd8\xff")
                ) or (
                    mime_type == "video/mp4"
                    and (len(header) < 12 or header[4:8] != b"ftyp")
                ):
                    raise StorageError(
                        "The downloaded media does not match its declared type."
                    )
                stream.seek(0)
                digest = hashlib.sha256()
                received = 0
                while chunk := stream.read(1024 * 1024):
                    received += len(chunk)
                    if received > MAX_MEDIA_BYTES:
                        raise StorageError(
                            "The downloaded media exceeds the 100 MB storage limit."
                        )
                    digest.update(chunk)
                if received != size:
                    raise StorageError(
                        "The downloaded media changed during persistence."
                    )
                stream.seek(0)
                cursor = connection.cursor()
                cursor.execute(
                    f"PUT file:///{filename} @{self.stage}/{media_id} "
                    "AUTO_COMPRESS=FALSE OVERWRITE=FALSE SOURCE_COMPRESSION=NONE",
                    file_stream=stream,
                    timeout=120,
                )
                rows = cursor.fetchall()
                if len(rows) != 1 or len(rows[0]) < 7:
                    raise StorageError("Snowflake did not confirm the media upload.")
                row = rows[0]
                if (
                    row[6] != "UPLOADED"
                    or row[2] not in (size, str(size))
                    or row[3] not in (size, str(size))
                    or not isinstance(row[1], str)
                    or row[1].rsplit("/", 1)[-1] != filename
                ):
                    raise StorageError(
                        "Snowflake did not confirm a complete media upload."
                    )
            cursor.execute(
                f"INSERT INTO {self.table} "
                "(POST_ID, MEDIA_ID, STAGE_PATH, SHA256, SIZE_BYTES, MIME_TYPE, METADATA) "
                "SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s)",
                (
                    post_id,
                    media_id,
                    stage_path,
                    digest.hexdigest(),
                    size,
                    mime_type,
                    metadata,
                ),
                timeout=120,
            )
            connection.commit()
        except Exception as exc:
            try:
                connection.rollback()
            except Exception as rollback_error:  # noqa: BLE001 - failed cleanup must preserve the safe original error
                logger.warning(
                    "Snowflake rollback failed (%s)", type(rollback_error).__name__
                )
            if isinstance(exc, StorageError):
                raise
            raise StorageError(
                "Snowflake could not persist the downloaded media and post."
            ) from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as close_error:  # noqa: BLE001 - cursor cleanup must preserve upload/commit outcome
                    logger.warning(
                        "Snowflake cursor close failed (%s)", type(close_error).__name__
                    )

    def close(self) -> None:
        """Release only a connection opened by this store."""
        if self._owns_connection and self._connection is not None:
            try:
                self._connection.close()
            except Exception as close_error:  # noqa: BLE001 - connector errors may contain credentials or signed URLs
                logger.warning(
                    "Snowflake connection close failed (%s)", type(close_error).__name__
                )
                raise StorageError(
                    "Could not close the Snowflake connection."
                ) from None
            finally:
                self._connection = None
                self._owns_connection = False
