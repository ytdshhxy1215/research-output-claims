from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Department(models.Model):
    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=120)

    def __str__(self) -> str:
        return self.name


class Researcher(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="researcher",
    )
    name = models.CharField(max_length=120)

    def __str__(self) -> str:
        return self.name


class AffiliationPeriod(models.Model):
    researcher = models.ForeignKey(Researcher, on_delete=models.PROTECT, related_name="affiliations")
    department = models.ForeignKey(Department, on_delete=models.PROTECT)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    is_primary = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gt=F("starts_on")),
                name="people_affiliation_positive_period",
            ),
            models.UniqueConstraint(
                fields=("researcher", "department", "starts_on"),
                name="people_affiliation_start_unique",
            ),
        ]
        indexes = [models.Index(fields=("researcher", "is_primary", "starts_on"))]


class DepartmentReviewer(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    department = models.ForeignKey(Department, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "department"), name="people_reviewer_unique"),
        ]
