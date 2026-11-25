"""PostgreSQL queue. All mutations lock scope before run; HTTP never runs here."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connection, transaction
from django.db.models import OuterRef, Q, Subquery

from .connector import IngestionError
from .models import SourceScope, SyncRun, SyncRunAttempt
from .normalization import ordered_events, persist_event

SOURCE = "fixture"
Status = SyncRun.Status
Outcome = SyncRunAttempt.Outcome


class LostLease(Exception):
    pass


class ReplayError(Exception):
    pass


@dataclass(frozen=True)
class Lease:
    scope_id: int
    run_id: int
    token: int


@dataclass(frozen=True)
class Checkpoint:
    lease: Lease
    stream: str
    release_id: str
    window_start: datetime
    window_end: datetime
    cursor: str
    last_event_at: datetime | None
    last_event_id: str
    page_limit: int


def _now():
    # One clock across workers; unlike CURRENT_TIMESTAMP, it advances within a transaction.
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]


def initialize_scopes():
    for stream in SourceScope.Stream.values:
        SourceScope.objects.get_or_create(source=SOURCE, stream=stream)


@transaction.atomic(durable=True)
def schedule(manifest, *, page_limit):
    created = []
    scopes = SourceScope.objects.select_for_update().filter(source=SOURCE).order_by("pk")
    for scope in scopes:
        upper_bound = manifest.upper_bounds[scope.stream]
        if upper_bound <= scope.watermark or scope.runs.exclude(status=Status.SUCCEEDED).exists():
            continue
        run = scope.runs.create(
            release_id=manifest.release_id,
            window_start=scope.watermark,
            window_end=upper_bound,
            page_limit=page_limit,
        )
        created.append(run.pk)
    return created


def _ready(now):
    return (
        Q(status=Status.PENDING)
        | Q(status=Status.RETRY, next_attempt_at__lte=now)
        | Q(status=Status.RUNNING, lease_until__lte=now)
    )


@transaction.atomic(durable=True)
def claim(*, lease_seconds):
    now = _now()
    ready_start = (
        SyncRun.objects.filter(scope_id=OuterRef("pk"))
        .filter(_ready(now))
        .order_by("window_start", "pk")
        .values("window_start")[:1]
    )
    scope = (
        SourceScope.objects.filter(source=SOURCE)
        .annotate(ready_start=Subquery(ready_start))
        .filter(ready_start__isnull=False)
        .order_by("ready_start", "pk")
        .select_for_update(of=("self",), skip_locked=True)
        .first()
    )
    if scope is None:
        return None
    now = _now()
    run = scope.runs.select_for_update().filter(_ready(now)).first()
    if run is None:
        # The scope may have changed between planning the scan and acquiring its lock.
        return None
    if run.status == Status.RUNNING:
        _finish_attempt(run, now, Outcome.EXPIRED, code="lease_expired", detail="lease expired")
    run.status = Status.RUNNING
    run.fencing_token += 1
    run.attempts += 1
    run.lease_until = now + timedelta(seconds=lease_seconds)
    run.next_attempt_at = None
    run.save(
        update_fields=("status", "fencing_token", "attempts", "lease_until", "next_attempt_at")
    )
    SyncRunAttempt.objects.create(
        run=run, fencing_token=run.fencing_token, started_at=now, start_cursor=run.cursor
    )
    return Lease(scope.pk, run.pk, run.fencing_token)


def _assert_owner(run, lease, now):
    if (
        run.status != Status.RUNNING
        or run.fencing_token != lease.token
        or run.lease_until is None
        or run.lease_until <= now
    ):
        raise LostLease(f"run {lease.run_id} lost lease {lease.token}")


def _lock_lease(lease):
    scope = SourceScope.objects.select_for_update().get(pk=lease.scope_id)
    run = scope.runs.select_for_update().get(pk=lease.run_id)
    now = _now()
    _assert_owner(run, lease, now)
    return scope, run, now


def _assert_checkpoint(run, checkpoint):
    if (run.cursor, run.last_event_at, run.last_event_id) != (
        checkpoint.cursor,
        checkpoint.last_event_at,
        checkpoint.last_event_id,
    ):
        raise LostLease(f"run {run.pk} checkpoint changed while processing a page")


def _finish_attempt(run, now, outcome, *, code="", detail="", payload=None):
    attempt = run.attempt_log.get(fencing_token=run.fencing_token, outcome=Outcome.RUNNING)
    attempt.finished_at = now
    attempt.outcome = outcome
    attempt.end_cursor = run.cursor
    attempt.error_code = code
    attempt.error_detail = detail
    attempt.rejected_payload = payload
    attempt.save(
        update_fields=(
            "finished_at",
            "outcome",
            "end_cursor",
            "error_code",
            "error_detail",
            "rejected_payload",
        )
    )


@transaction.atomic(durable=True)
def renew(lease, *, lease_seconds):
    scope, run, now = _lock_lease(lease)
    run.lease_until = now + timedelta(seconds=lease_seconds)
    run.save(update_fields=("lease_until",))
    return Checkpoint(
        lease,
        scope.stream,
        run.release_id,
        run.window_start,
        run.window_end,
        run.cursor,
        run.last_event_at,
        run.last_event_id,
        run.page_limit,
    )


def commit_page(checkpoint, page, *, lease_seconds):
    # Validate and hash before taking locks. DB writes and checkpoint are one transaction.
    events = ordered_events(page, checkpoint)
    with transaction.atomic(durable=True):
        scope, run, _ = _lock_lease(checkpoint.lease)
        _assert_checkpoint(run, checkpoint)
        if scope.watermark != run.window_start and scope.watermark < run.window_end:
            raise IngestionError("window_gap", "window no longer follows the scope watermark")
        for event in events:
            persist_event(run, event)
        now = _now()
        _assert_owner(run, checkpoint.lease, now)
        run.pages_committed += 1
        run.events_committed += len(events)
        if events:
            run.last_event_at, run.last_event_id = events[-1].order_key
        if page.next_cursor:
            run.cursor = page.next_cursor
            run.lease_until = now + timedelta(seconds=lease_seconds)
        else:
            run.status = Status.SUCCEEDED
            run.completed_at = now
            run.lease_until = None
            run.error_code = run.error_detail = ""
            _finish_attempt(run, now, Outcome.SUCCEEDED)
            run.cursor = ""
            if scope.watermark == run.window_start:
                scope.watermark = run.window_end
                scope.save(update_fields=("watermark",))
            # A historical replay must never rewind (or jump) the main watermark.
        run.save(
            update_fields=(
                "status",
                "lease_until",
                "cursor",
                "last_event_at",
                "last_event_id",
                "pages_committed",
                "events_committed",
                "completed_at",
                "error_code",
                "error_detail",
            )
        )
    return not page.next_cursor


@transaction.atomic(durable=True)
def fail(checkpoint, error):
    _, run, now = _lock_lease(checkpoint.lease)
    _assert_checkpoint(run, checkpoint)
    run.status = Status.RETRY if error.retryable else Status.BLOCKED
    run.lease_until = None
    run.error_code, run.error_detail = error.code, str(error)[:2000]
    if error.retryable:
        seconds = error.retry_after or min(300, 2 ** min(run.attempts, 9))
        run.next_attempt_at = now + timedelta(seconds=seconds)
    else:
        run.next_attempt_at = None
    _finish_attempt(
        run,
        now,
        Outcome.RETRY if error.retryable else Outcome.BLOCKED,
        code=run.error_code,
        detail=run.error_detail,
        payload=error.payload,
    )
    run.save(
        update_fields=("status", "lease_until", "error_code", "error_detail", "next_attempt_at")
    )


@transaction.atomic(durable=True)
def requeue(run_id):
    try:
        scope_id = SyncRun.objects.values_list("scope_id", flat=True).get(
            pk=run_id, scope__source=SOURCE
        )
    except SyncRun.DoesNotExist as exc:
        raise ReplayError(f"fixture run {run_id} does not exist") from exc
    scope = SourceScope.objects.select_for_update().get(pk=scope_id)
    run = scope.runs.select_for_update().get(pk=run_id)
    if run.status not in (Status.RETRY, Status.BLOCKED, Status.SUCCEEDED):
        raise ReplayError("only retrying, blocked or succeeded runs can be replayed")
    if scope.runs.exclude(pk=run_id).exclude(status=Status.SUCCEEDED).exists():
        raise ReplayError("finish the open run for this scope before replaying a historical run")
    if run.status == Status.SUCCEEDED:
        run.cursor = run.last_event_id = ""
        run.last_event_at = run.completed_at = None
        run.pages_committed = run.events_committed = 0
    run.status = Status.PENDING
    run.next_attempt_at = None
    run.error_code = run.error_detail = ""
    run.save(
        update_fields=(
            "status",
            "next_attempt_at",
            "cursor",
            "last_event_at",
            "last_event_id",
            "completed_at",
            "pages_committed",
            "events_committed",
            "error_code",
            "error_detail",
        )
    )
