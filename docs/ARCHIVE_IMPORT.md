# Transcript archive import (version 1)

Run from the repository root with the existing backend Python environment:

```sh
python -m backend.scripts.import_source_archive /path/to/archive.jsonl
```

Uses the existing TigerStore environment configuration and schema initialization.
The command writes to that configured database: use an explicitly configured local
SQLite database for testing. It never fetches source or recording URLs.

Each physical line is one UTF-8 JSON object with exactly these required fields:

```json
{"version":1,"source_url":"https://publisher.example/speech","text":"Exact public transcript text.","metadata":{"title":"Speech","publisher":"Publisher","published_at":null,"language":"en"},"provenance":{"transcript_source_url":"https://publisher.example/speech","acquired_at":"2026-10-10T19:00:00Z","rights_status":"public"}}
```

`version` must be integer 1. Text must be nonempty, at most 1,000,000 UTF-8 bytes;
the entire physical line must be at most 2,000,000 bytes including its newline.
Metadata and provenance are required objects, each at most 32,768 UTF-8 bytes
when JSON encoded; values must be strings or null. Empty objects are allowed.
Unknown fields are rejected. Metadata permits `title`, `publisher`, `published_at`,
`language`, `recording_url`. Provenance permits `sha256`, `transcript_source_url`,
`acquired_at`, `rights_status`, `recording_link_basis`, `event_date`,
`publication_date`. Dates and rights labels are
publisher/archive claims, not independently verified facts.

`provenance.event_date` identifies the event's claimed calendar day;
`provenance.publication_date` identifies the transcript's claimed publication day.
Both are optional, accept null for unknown, and otherwise require a real calendar
date in exact `YYYY-MM-DD` format (including leap-year validation). They are
distinct claims and need not be the same day. They carry day precision with no
inferred timezone or time of day. Preserve a known actual publication timestamp
separately in `metadata.published_at`; leave that null or omitted when only a day
is known. The importer never converts either date to an invented midnight UTC
timestamp or copies an event date into publication metadata.

The importer computes SHA-256 over the exact UTF-8 text without trimming or
normalization. An optional supplied `sha256` must match. Public HTTP(S) URLs use
the existing retrieval validator (no credentials, local hosts, private IP literals,
or unusual ports). This is syntactic validation, not DNS resolution or evidence of
publisher authenticity. The operator is responsible for lawful public provenance.

An optional `metadata.recording_url` requires
`provenance.recording_link_basis="publisher_link"`: include it only when the
publisher actually links that recording. Otherwise omit it; storage uses the
source page URL. No media asset, word timing, or start/end seconds are invented.
All imported documents use `timing_basis="unavailable"`; storage leaves times null.

Processing uses bounded line reads, holds only one record at a time, and delegates
each atomic document/chunk transaction and durable URL/content-hash idempotency
to TigerStore. Restart by rerunning the entire file: committed content is unchanged,
failed records are retried, and changed text has a new content hash. No process-local
checkpoint or second datastore is needed. This supports 100,000 records without
loading the archive into memory; throughput depends on per-record DB transactions.

Each rejected record produces one sanitized JSON error on stderr with its physical
line number and short corrective feedback. Processing continues. Stdout receives
`records`, `inserted`, `unchanged`, `failed` counts; inserted counts documents, not
chunks. Exit status is 1 for any rejected record or startup/file failure, otherwise
0. Errors never echo text, URLs, raw database exceptions, or credentials. A fatal
file/read failure yields a sanitized error without a completed count summary;
rerun safely after repair. Blank lines are invalid records. No automatic network
retry is attempted; operator reruns avoid hiding persistent configuration failures.
