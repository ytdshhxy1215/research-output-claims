from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.sources.models import SourceScope, SyncReplay, SyncRun

pytestmark = pytest.mark.django_db(transaction=True)


def test_operator_replay_is_scoped_csrf_protected_and_audited(django_user_model):
    scope = SourceScope.objects.create(stream="works")
    run = SyncRun.objects.create(
        scope=scope, release_id="batch-1", window_start=scope.watermark,
        window_end=scope.watermark + timedelta(days=1), status=SyncRun.Status.BLOCKED,
        fencing_token=5, cursor="saved-page", error_code="http_contract",
    )
    operator = django_user_model.objects.create_user(username="operator")
    operator.user_permissions.add(Permission.objects.get(codename="view_syncrun"))
    client = Client(enforce_csrf_checks=True)
    client.force_login(operator)
    detail = reverse("sync_detail", args=[run.pk])
    replay = reverse("sync_replay", args=[run.pk])
    assert "排队重放" not in client.get(detail).content.decode()
    assert client.post(replay, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value).status_code == 403
    operator.user_permissions.add(Permission.objects.get(codename="request_syncreplay"))
    assert client.get(replay).status_code == 405
    assert client.post(replay, {"token": 5, "reason": "补齐模拟源"}).status_code == 403
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    assert client.post(replay, {"token": 5, "reason": " "}, **headers).status_code == 400
    assert client.post(replay, {"token": 5, "reason": "补齐模拟源"}, **headers).status_code == 302
    assert client.post(replay, {"token": 5, "reason": "重复提交"}, **headers).status_code == 409
    run.refresh_from_db()
    scope.refresh_from_db()
    assert (run.status, run.cursor, scope.watermark) == ("pending", "saved-page", run.window_start)
    event = SyncReplay.objects.get(run=run)
    assert (event.actor, event.reason, event.previous_status) == (operator, "补齐模拟源", "blocked")
    assert "补齐模拟源" in client.get(detail).content.decode()


def test_overdue_and_audit_filters_respect_claim_access(django_user_model, client):
    from apps.catalog.models import ContributionSlot, Work
    from apps.claims.models import ClaimCase, ClaimEvent
    from apps.people.models import Department, DepartmentReviewer, Researcher

    reviewer = django_user_model.objects.create_user(username="reviewer")
    teacher = django_user_model.objects.create_user(username="teacher")
    researcher = Researcher.objects.create(user=teacher, name="教师")
    alpha = Department.objects.create(code="a", name="甲院")
    beta = Department.objects.create(code="b", name="乙院")
    DepartmentReviewer.objects.create(user=reviewer, department=alpha)
    work = Work.objects.create(kind="paper", identifier="10.5555/overdue", title="样例")
    for position, department, age in [(1, alpha, 7), (2, alpha, 6), (3, beta, 8)]:
        slot = ContributionSlot.objects.create(work=work, position=position, name="教师",
                                               source_slot_id=str(position))
        case = ClaimCase.objects.create(slot=slot, researcher=researcher, review_department=department,
                                        submitted_at=timezone.now() - timedelta(days=age))
        if position == 1:
            first = case
    for kind, reason in [("submitted", "提交证明"), ("reassigned", "内部转派原因")]:
        ClaimEvent.objects.create(case=first, actor=reviewer, kind=kind, reason=reason, to_status="pending")
    client.force_login(reviewer)
    assert "1 条可处理申请已等待至少 7 天" in client.get(reverse("claims:index")).content.decode()
    detail = reverse("claims:detail", args=[first.pk])
    assert "内部转派原因" in client.get(detail, {"event": "reassigned"}).content.decode()
    assert "内部转派原因" not in client.get(detail, {"event": "submitted"}).content.decode()
    client.force_login(teacher)
    body = client.get(detail, {"event": "reassigned"}).content.decode()
    assert "内部转派原因" not in body and 'value="reassigned"' not in body


def test_simultaneous_replay_requests_create_one_audit_entry(django_user_model):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connections

    from apps.sources.queue import ReplayError, requeue

    scope = SourceScope.objects.create(stream="patents")
    run = SyncRun.objects.create(
        scope=scope, release_id="batch-1", window_start=scope.watermark,
        window_end=scope.watermark + timedelta(days=1), status=SyncRun.Status.BLOCKED,
        fencing_token=7,
    )
    actor = django_user_model.objects.create_user(username="concurrent-operator")
    actor.user_permissions.add(*Permission.objects.filter(
        content_type__app_label="sources", codename__in=("view_syncrun", "request_syncreplay"),
    ))
    barrier = Barrier(2, timeout=10)

    def replay():
        close_old_connections()
        try:
            user = django_user_model.objects.get(pk=actor.pk)
            barrier.wait()
            requeue(run.pk, actor=user, reason="并发恢复", expected_token=7)
            return True
        except ReplayError:
            return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: replay(), range(2))) == [False, True]
    assert SyncReplay.objects.filter(run=run, token=7).count() == 1


def test_cli_replay_requires_an_authorized_actor_and_a_reason(django_user_model):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    scope = SourceScope.objects.create(stream="works")
    run = SyncRun.objects.create(
        scope=scope, release_id="batch-1", window_start=scope.watermark,
        window_end=scope.watermark + timedelta(days=1), status=SyncRun.Status.BLOCKED,
    )
    user = django_user_model.objects.create_user(username="ordinary")
    with pytest.raises(CommandError, match="requires --actor and --reason"):
        call_command("run_sync_worker", replay=run.pk)
    with pytest.raises(CommandError, match="does not have replay permission"):
        call_command("run_sync_worker", replay=run.pk, actor=user.username, reason="维护")
    run.refresh_from_db()
    assert run.status == SyncRun.Status.BLOCKED
    assert not SyncReplay.objects.filter(run=run).exists()
