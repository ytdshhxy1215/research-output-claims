from django.db import models


class Work(models.Model):
    class Kind(models.TextChoices):
        PAPER = "paper", "Paper"
        PATENT = "patent", "Patent"

    title = models.CharField(max_length=500)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    identifier = models.CharField(max_length=255)
    published_on = models.DateField(null=True, blank=True)
    granted_on = models.DateField(null=True, blank=True)
    # Ordering of the current normalized view, not an ingestion watermark.
    source_updated_at = models.DateTimeField(null=True, blank=True)
    source_event_id = models.CharField(max_length=128, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("kind", "identifier"), name="catalog_work_identity"),
        ]

    def __str__(self):
        return self.title


class ContributionSlot(models.Model):
    work = models.ForeignKey(Work, on_delete=models.CASCADE, related_name="slots")
    position = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    source_slot_id = models.CharField(max_length=128)
    affiliation = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("position", "pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(position__gte=1), name="catalog_slot_1_based"
            ),
            models.UniqueConstraint(
                fields=("work", "source_slot_id"), name="catalog_slot_source_identity"
            ),
        ]

    def __str__(self):
        return self.name
