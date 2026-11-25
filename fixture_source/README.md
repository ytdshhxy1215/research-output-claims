# Synthetic ingestion fixture

Start it with `python -m fixture_source` (binds port 8081, `GET /healthz`). It starts
at `batch-1`; to start at `batch-2` instead, pass `--release-id batch-2` or set
`FIXTURE_RELEASE_ID=batch-2`. Release and fault controls are in-memory and reset
on server restart. Do not expose this unauthenticated fixture to external traffic.

| Release | Exclusive upper bound (both streams) | Events |
| --- | --- | --- |
| `batch-1` | `2025-02-02T00:00:00Z` | DOI `10.5555/p1` (published `2025-01-09`): 林岚/`p1-a1`/`alpha`, 陈星/`p1-a2`/`乙院`; patent `CN:FIXTURE-2025-0001` (`2025-01-16`) with 陈星/`pat1-i1`/`乙院` |
| `batch-2` | `2025-02-03T00:00:00Z` | P1 revises 林岚's affiliation to `甲院` and repeats that version; DOI `10.5555/p2` (published `2025-01-23`) with 周宁/`p2-a1`/`甲院`; patent grant `2025-01-30` (same application, distinct A/B source records) |

`GET /v1/manifest` returns `release_id` and `streams.{works,patents}.upper_bound`.
Request `GET /v1/{works|patents}` with `release_id`, UTC `from` (inclusive), UTC
`to` (exclusive), optional `cursor` and `limit` (1–100). Responses contain `events`
and `next_cursor` (`null` on the final page). Each event has `event_id`,
`external_id`, UTC `updated_at`, and a versioned `record` (`doi`/`authors` or
patent application/document keys/`inventors`). The three second-batch paper events
share a timestamp and are ordered by `event_id`. A pinned prior release remains
readable after publication of the next one.

Fixture-only controls (JSON POST bodies):

- `POST /__fixture/release` with `{"release_id":"batch-2"}` publishes batch 2.
- `POST /__fixture/failure` with `{"stream":"works","cursor":"<next_cursor>","status":429,"count":1,"retry_after":5}` injects one rate limit at that cursor. Omit `cursor` for the first page; omit `status` for 503; use `count: 0` to clear an injection. Get the second-page cursor by fetching the first page with the same release and time window.

With PostgreSQL migrated, set `FIXTURE_SOURCE_URL` to the fixture base URL and run
`python manage.py run_sync_worker --once --page-limit 1` to poll and drain due
attempts. Omit `--once` for continuous polling. The page size is pinned on run
creation. `python manage.py run_sync_worker --replay RUN_ID` retries a blocked or
retrying window from its checkpoint, or rescans a succeeded window; the command
queues the replay and drains due work once. `--once` reports attempts claimed,
not sync success: inspect `SyncRun.status` and `SyncRun.attempt_log` for outcomes.

P0 polls contiguous windows only; an already released snapshot may cover both
batches if the first poll starts at batch 2. A blocked window stops newer windows
for that stream until explicit replay; the other stream can progress. The worker
preserves slots across affiliation/reorder updates and blocks ambiguous removals
or renames instead of transferring claims. It does not implement deletion,
filter-scope reconfiguration, an approved-skip workflow, or matching job enqueue.
