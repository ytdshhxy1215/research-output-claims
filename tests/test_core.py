from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher


def test_landing_and_liveness(client):
    response = client.get(reverse("home"))
    assert response.status_code == 200
    assert "学术成果智能归集与认领系统" in response.content.decode()
    assert client.get(reverse("health")).json() == {"status": "ok"}
    assert client.post(reverse("health")).status_code == 405


@pytest.mark.django_db
def test_readiness_and_seed_idempotence(client, settings):
    settings.DEMO_PASSWORD = "synthetic-local-only-123"
    assert client.get(reverse("ready")).json() == {"status": "ready"}
    call_command("seed_demo", verbosity=0)
    call_command("seed_demo", verbosity=0)
    assert Researcher.objects.count() == 3
    assert DepartmentReviewer.objects.count() == 2
    assert Department.objects.count() == 2
    assert AffiliationPeriod.objects.filter(researcher__user__username="lin").count() == 2
    school = get_user_model().objects.get(username="school-review")
    assert school.groups.filter(name="school-reviewers").exists() and not school.is_staff
    assert client.login(username="lin", password=settings.DEMO_PASSWORD)
    assert client.get(reverse("sync_operations")).status_code == 403
    assert client.login(username="source-ops", password=settings.DEMO_PASSWORD)
    assert client.get(reverse("sync_operations")).status_code == 200


@pytest.mark.django_db
def test_historical_affiliation_rejects_empty_range():
    user = get_user_model().objects.create(username="range-test")
    researcher = Researcher.objects.create(user=user, name="样例")
    department = Department.objects.create(name="样例学院", code="sample")
    with pytest.raises(IntegrityError), transaction.atomic():
        AffiliationPeriod.objects.create(
            researcher=researcher, department=department,
            starts_on=date(2026, 1, 1), ends_on=date(2026, 1, 1),
        )
