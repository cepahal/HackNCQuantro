"""Bounded, restartable JSONL transcript import through TigerStore (no URL fetching)."""

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import BinaryIO, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.external_apis.retrieve_post import RetrievalError, _validate_url
from backend.services.shared.config import load_environment
from backend.services.storage.tiger import TigerStore

MAX_LINE_BYTES = 2_000_000
MAX_TEXT_BYTES = 1_000_000
MAX_CONTEXT_BYTES = 32_768
PROVENANCE_KEYS = {
    "sha256",
    "transcript_source_url",
    "acquired_at",
    "rights_status",
    "recording_link_basis",
    "event_date",
    "publication_date",
}
METADATA_KEYS = {"title", "publisher", "published_at", "language", "recording_url"}


def validate_record(value: object) -> dict:
    """Validate version 1 and attach the hash of the exact, unmodified UTF-8 text."""
    if not isinstance(value, dict) or set(value) != {
        "version",
        "source_url",
        "text",
        "metadata",
        "provenance",
    }:
        raise ValueError("Use the documented version 1 record fields.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Use archive format version 1.")
    text = value["text"]
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > MAX_TEXT_BYTES
    ):
        raise ValueError(
            "Provide nonempty transcript text within the documented size limit."
        )
    source_url, _ = _validate_url(value["source_url"], allow_webpage=True)
    metadata, provenance = value["metadata"], value["provenance"]
    for context, keys in ((metadata, METADATA_KEYS), (provenance, PROVENANCE_KEYS)):
        if not isinstance(context, dict) or set(context) - keys:
            raise ValueError("Use only documented metadata and provenance fields.")
        if any(v is not None and not isinstance(v, str) for v in context.values()):
            raise ValueError("Metadata and provenance values must be strings or null.")
        if (
            len(json.dumps(context, ensure_ascii=False).encode("utf-8"))
            > MAX_CONTEXT_BYTES
        ):
            raise ValueError(
                "Reduce metadata or provenance to the documented size limit."
            )
    provenance = dict(provenance)
    for name in ("event_date", "publication_date"):
        claimed_date = provenance.get(name)
        # fromisoformat also accepts compact/week dates; require calendar day precision.
        if (
            claimed_date is not None
            and date.fromisoformat(claimed_date).isoformat() != claimed_date
        ):
            raise ValueError("Archive dates must use YYYY-MM-DD calendar dates.")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if provenance.get("sha256") not in {None, digest}:
        raise ValueError(
            "The supplied SHA-256 must match the exact UTF-8 transcript text."
        )
    provenance["sha256"] = digest
    if provenance.get("transcript_source_url") is not None:
        provenance["transcript_source_url"], _ = _validate_url(
            provenance["transcript_source_url"],
            allow_webpage=True,
        )
    metadata = dict(metadata)
    if metadata.get("recording_url") is not None:
        metadata["recording_url"], _ = _validate_url(
            metadata["recording_url"], allow_webpage=True
        )
        if provenance.get("recording_link_basis") != "publisher_link":
            raise ValueError("A recording URL requires publisher_link provenance.")
    return {
        "source_url": source_url,
        "text": text,
        "metadata": metadata,
        "provenance": provenance,
        "timing_basis": "unavailable",
    }


def import_archive(
    stream: BinaryIO, store: TigerStore, errors: TextIO
) -> dict[str, int]:
    """Continue past bad records; bound reads even when a physical line is oversized."""
    counts = {"records": 0, "inserted": 0, "unchanged": 0, "failed": 0}
    line_number = 0
    while raw := stream.readline(MAX_LINE_BYTES + 1):
        line_number += 1
        counts["records"] += 1
        if len(raw) > MAX_LINE_BYTES:
            while not raw.endswith(b"\n"):
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
            message = (
                "Record exceeds the JSONL byte limit. Split or shorten this record."
            )
        else:
            try:
                record = validate_record(json.loads(raw.decode("utf-8")))
            except (
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
                RetrievalError,
            ):
                message = "Invalid archive record. Check version, public URLs, field types, sizes and provenance."
            else:
                try:
                    inserted = store.upsert_transcript_document(**record)
                except Exception:  # noqa: BLE001 -- sanitize the per-record database boundary
                    # Driver errors may contain connection credentials or transcript parameters.
                    message = "Storage write failed. Check database readiness and retry this archive."
                else:
                    counts["inserted" if inserted else "unchanged"] += 1
                    continue
        counts["failed"] += 1
        print(json.dumps({"line": line_number, "error": message}), file=errors)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive", type=Path, help="UTF-8 JSONL archive; never fetched remotely"
    )
    args = parser.parse_args(argv)
    store = None
    try:
        # Open the input before connecting, to avoid a schema operation for a missing file.
        with args.archive.open("rb") as stream:
            load_environment()
            store = TigerStore.from_env()
            store.ensure_schema()
            counts = import_archive(stream, store, sys.stderr)
    except Exception:  # noqa: BLE001 -- never expose configuration/driver secrets in CLI output
        print(
            json.dumps(
                {
                    "error": "Import could not complete. Check the local file and database configuration; retry safely."
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001 -- disposal errors may also contain driver secrets
                print(
                    json.dumps(
                        {
                            "error": "Database cleanup failed. Retry safely after checking database readiness."
                        }
                    ),
                    file=sys.stderr,
                )
                return 1
    print(json.dumps(counts))
    return int(counts["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
