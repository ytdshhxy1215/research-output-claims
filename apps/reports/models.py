from uuid import uuid4

from django.db import models


class ReportSnapshot(models.Model):
    """One append-only document; versioned report facts never join back to live objects."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    captured_at = models.DateTimeField()
    created_by_id = models.BigIntegerField()
    policy = models.CharField(max_length=16)
    policy_label = models.CharField(max_length=80)
    implementation_version = models.CharField(max_length=32)
    filters = models.JSONField()
    payload = models.JSONField()

    class Meta:
        ordering = ("-captured_at", "-pk")
