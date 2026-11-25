from datetime import UTC, datetime

from django.db import models


def initial_watermark():
    return datetime(1970, 1, 1, tzinfo=UTC)


class SourceScope(models.Model):
    class Stream(models.TextChoices):
        WORKS = "works", "Works"
        PATENTS = "patents", "Patents"

    source = models.CharField(max_length=40, default="fixture")
    stream = models.CharField(max_length=12, choices=Stream.choices)
    watermark = models.DateTimeField(default=initial_watermark)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source", "stream"), name="sources_scope_identity"),
        ]


class SyncRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        RETRY = "retry", "Retry"
        BLOCKED = "blocked", "Blocked"
        SUCCEEDED = "succeeded", "Succeeded"

    scope = models.ForeignKey(SourceScope, on_delete=models.PROTECT, related_name="runs")
    release_id = models.CharField(max_length=128)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    cursor = models.TextField(blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_event_id = models.CharField(max_length=128, blank=True)
    pages_committed = models.PositiveIntegerField(default=0)
    events_committed = models.PositiveIntegerField(default=0)
    page_limit = models.PositiveIntegerField(default=100)
    attempts = models.PositiveIntegerField(default=0)
    fencing_token = models.PositiveBigIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    error_detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="sources_run_ready_idx"),
            models.Index(fields=("status", "lease_until"), name="sources_run_expired_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "window_start", "window_end"), name="sources_run_window"
            ),
            models.UniqueConstraint(
                fields=("scope",),
                condition=~models.Q(status="succeeded"),
                name="sources_one_open_run_per_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(window_end__gt=models.F("window_start")),
                name="sources_run_nonempty_window",
            ),
        ]


class SyncRunAttempt(models.Model):
    class Outcome(models.TextChoices):
        RUNNING = "running", "Running"
        EXPIRED = "expired", "Lease expired"
        RETRY = "retry", "Retry"
        BLOCKED = "blocked", "Blocked"
        SUCCEEDED = "succeeded", "Succeeded"

    run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="attempt_log")
    fencing_token = models.PositiveBigIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=12, choices=Outcome.choices, default=Outcome.RUNNING)
    start_cursor = models.TextField(blank=True)
    end_cursor = models.TextField(blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    error_detail = models.TextField(blank=True)
    rejected_payload = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("run", "fencing_token"), name="sources_attempt_fence"),
        ]


class SourceRecord(models.Model):
    scope = models.ForeignKey(SourceScope, on_delete=models.PROTECT, related_name="records")
    external_id = models.CharField(max_length=128)
    work = models.ForeignKey(
        "catalog.Work", on_delete=models.PROTECT, related_name="source_records"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "external_id"), name="sources_record_identity"
            ),
        ]


class SourceRecordRevision(models.Model):
    record = models.ForeignKey(SourceRecord, on_delete=models.PROTECT, related_name="revisions")
    run = models.ForeignKey(SyncRun, on_delete=models.PROTECT, related_name="revisions")
    content_hash = models.CharField(max_length=64)
    event_id = models.CharField(max_length=128)
    updated_at = models.DateTimeField()
    fetched_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("record", "content_hash"), name="sources_revision_content"
            ),
        ]
