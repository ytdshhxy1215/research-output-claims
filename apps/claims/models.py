from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class ClaimCase(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待初审"
        CONFLICT = "conflict", "署名冲突待处理"
        REJECTED = "rejected", "初审驳回"
        APPEALED = "appealed", "申诉待复核"
        APPROVED = "approved", "已核准"
        UPHELD = "upheld", "维持驳回"
        REVOKED = "revoked", "归属已撤销"

    slot = models.ForeignKey("catalog.ContributionSlot", on_delete=models.PROTECT)
    researcher = models.ForeignKey("people.Researcher", on_delete=models.PROTECT)
    review_department = models.ForeignKey(
        "people.Department", null=True, blank=True, on_delete=models.PROTECT,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    evidence = models.CharField(max_length=4000)
    evidence_identifier = models.CharField(max_length=255, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    initial_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("slot", "researcher"), name="claims_one_case_per_slot_researcher",
            ),
        ]
        indexes = [models.Index(fields=("review_department", "status", "submitted_at"))]
        ordering = ("-submitted_at", "-pk")


class Attribution(models.Model):
    """Confirmed attribution. Validity is [valid_from, valid_until); null means active."""

    slot = models.ForeignKey("catalog.ContributionSlot", on_delete=models.PROTECT)
    researcher = models.ForeignKey("people.Researcher", on_delete=models.PROTECT)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("slot",), condition=Q(valid_until__isnull=True),
                name="claims_one_active_attribution_per_slot",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=F("valid_from")),
                name="claims_attribution_valid_period",
            ),
        ]


class ClaimEvent(models.Model):
    class Kind(models.TextChoices):
        SUBMITTED = "submitted", "提交申请"
        REASSIGNED = "reassigned", "转派审核"
        CONFLICT = "conflict", "标记署名冲突"
        APPROVED = "approved", "初审核准"
        REJECTED = "rejected", "初审驳回"
        APPEALED = "appealed", "提交申诉"
        OVERTURNED = "overturned", "复核改判核准"
        UPHELD = "upheld", "复核维持驳回"
        REVOKED = "revoked", "撤销有效归属"

    case = models.ForeignKey(ClaimCase, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    reason = models.CharField(max_length=4000)
    from_status = models.CharField(max_length=16, choices=ClaimCase.Status.choices, blank=True)
    to_status = models.CharField(max_length=16, choices=ClaimCase.Status.choices)
    from_department = models.ForeignKey(
        "people.Department", null=True, on_delete=models.PROTECT, related_name="+",
    )
    to_department = models.ForeignKey(
        "people.Department", null=True, on_delete=models.PROTECT, related_name="+",
    )
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("occurred_at", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("case", "kind"), condition=Q(kind__in=("submitted", "appealed")),
                name="claims_once_for_submit_and_appeal",
            ),
        ]


class DismissedRecommendation(models.Model):
    """Personal presentation preference, not a decision about the slot."""

    slot = models.ForeignKey("catalog.ContributionSlot", on_delete=models.CASCADE)
    researcher = models.ForeignKey("people.Researcher", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("researcher", "slot"), name="claims_one_dismissal_per_researcher_slot",
            ),
        ]
