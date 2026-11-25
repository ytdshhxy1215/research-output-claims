from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import ContributionSlot, Work
from apps.claims import services
from apps.claims.models import Attribution, ClaimCase, ClaimEvent, DismissedRecommendation
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher
from apps.sources.models import SourceRecord, SourceRecordRevision, SourceScope, SyncRun

pytestmark = pytest.mark.django_db
Status = ClaimCase.Status
Kind = ClaimEvent.Kind


@pytest.fixture(autouse=True)
def unhashed_static(settings, tmp_path):
    settings.STATIC_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def account(username, *, teacher=False, school=False, **kwargs):
    user = get_user_model().objects.create_user(username=username, **kwargs)
    if teacher:
        Researcher.objects.create(user=user, name=username)
    if school:
        group, _ = Group.objects.get_or_create(name="school-reviewers")
        user.groups.add(group)
    return user


@pytest.fixture
def casework():
    work = Work.objects.create(
        identifier="10.5555/p1", title="P1 Joint work", kind=Work.Kind.PAPER,
        published_on=date(2025, 10, 10),
    )
    first, second = [
        ContributionSlot.objects.create(
            work=work, position=position, name=name, source_slot_id=f"p1-{position}",
        )
        for position, name in [(1, "林岚"), (2, "陈星")]
    ]
    dept_a = Department.objects.create(code="a", name="甲院")
    dept_b = Department.objects.create(code="b", name="乙院")
    reviewer_a = account("reviewer-a")
    reviewer_b = account("reviewer-b")
    school = account("school-independent", school=True)
    DepartmentReviewer.objects.create(user=reviewer_a, department=dept_a)
    DepartmentReviewer.objects.create(user=reviewer_b, department=dept_b)
    teacher = account("teacher", teacher=True)
    AffiliationPeriod.objects.create(
        researcher=teacher.researcher, department=dept_b,
        starts_on=timezone.localdate() - timedelta(days=20),
    )
    return SimpleNamespace(
        work=work, first=first, second=second, teacher=teacher, dept_a=dept_a, dept_b=dept_b,
        reviewer_a=reviewer_a, reviewer_b=reviewer_b, school=school,
    )


def url(name, obj=None):
    return reverse(f"claims:{name}", args=[obj.pk] if obj is not None else [])


def get_as(user, name, obj=None, query=None):
    client = Client()
    client.force_login(user)
    return client.get(url(name, obj), query or {})


def post_as(user, name, obj, **data):
    client = Client()
    client.force_login(user)
    return client.post(url(name, obj), data)


def submitted(world, *, actor=None, slot=None, evidence="姓名和机构对应。"):
    return services.submit_claim(
        actor=actor or world.teacher, slot_id=(slot or world.first).pk, evidence=evidence,
    )


def test_doi_search_submit_evidence_and_csrf(casework):
    w = casework
    anon = Client()
    assert anon.get(url("index")).status_code == 302
    assert "当前成果库未找到" in get_as(w.teacher, "index", query={
        "identifier": "10.5555/missing",
    }).content.decode()
    lookup = get_as(w.teacher, "index", query={"identifier": "https://doi.org/10.5555/P1"})
    assert lookup.status_code == 200
    assert url("submit", w.first) in lookup.content.decode()
    assert url("submit", w.second) in lookup.content.decode()
    assert get_as(w.reviewer_a, "index", query={"identifier": "10.5555/p1"}).status_code == 403
    assert post_as(w.reviewer_a, "submit", w.first, evidence="Impersonation").status_code == 403
    assert post_as(w.teacher, "submit", w.first, evidence=" ").status_code == 400
    assert not ClaimCase.objects.exists()

    client = Client(enforce_csrf_checks=True)
    client.force_login(w.teacher)
    form = client.get(url("submit", w.first))
    assert 'name="csrfmiddlewaretoken"' in form.content.decode()
    forbidden = client.post(url("submit", w.first), {"evidence": "No CSRF"})
    assert forbidden.status_code == 403
    data = {
        "evidence": "  核对 <private>  ", "evidence_identifier": "ID-1",
        "slot_id": w.second.pk, "researcher_id": w.reviewer_a.pk,
    }
    assert client.post(
        url("submit", w.first), data, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    ).status_code == 302
    case = ClaimCase.objects.get()
    assert (case.slot, case.researcher, case.review_department) == (w.first, w.teacher.researcher, w.dept_b)
    assert case.evidence == "核对 <private>"
    assert case.evidence_identifier == "ID-1"
    event = case.events.get()
    assert (event.kind, event.actor, event.to_department) == (Kind.SUBMITTED, w.teacher, w.dept_b)
    html = client.get(url("detail", case)).content.decode()
    assert "核对 &lt;private&gt;" in html and "<private>" not in html
    assert "no-store" in client.get(url("detail", case)).headers["Cache-Control"]
    assert client.get(url("submit", w.first)).url == url("detail", case)


@pytest.mark.parametrize("document", ["CNFIXTURE0001A", "cnfixture0001b"])
def test_catalog_patent_document_aliases_find_one_work(casework, document):
    w = casework
    patent = Work.objects.create(kind=Work.Kind.PATENT, identifier="CN:FIXTURE-1", title="T1")
    slot = ContributionSlot.objects.create(
        work=patent, position=1, source_slot_id="t1-1", name="林岚",
    )
    now = timezone.now()
    scope = SourceScope.objects.create(stream=SourceScope.Stream.PATENTS)
    run = SyncRun.objects.create(
        scope=scope, release_id="test", window_start=now - timedelta(days=1), window_end=now,
    )
    record = SourceRecord.objects.create(scope=scope, external_id="t1", work=patent)
    for suffix in ("A", "B"):
        SourceRecordRevision.objects.create(
            record=record, run=run, content_hash=f"test-{suffix}", event_id=suffix, updated_at=now,
            payload={"document_number": f"CNFIXTURE0001{suffix}", "hidden": "private-source"},
        )
    page = get_as(w.teacher, "index", query={"identifier": document}).content.decode()
    assert page.count(url("submit", slot)) == 1
    assert "CN:FIXTURE-1" in page and "private-source" not in page


def test_biz_01_personal_recommendation_dismissal_allows_direct_doi_claim(casework):
    w = casework
    w.teacher.researcher.name = "林岚"
    w.teacher.researcher.save(update_fields=["name"])
    AffiliationPeriod.objects.filter(researcher=w.teacher.researcher).update(
        starts_on=date(2020, 1, 1),
    )
    w.first.affiliation = w.dept_b.name
    w.first.save(update_fields=["affiliation"])
    w.second.name, w.second.affiliation = "林岚", w.dept_a.name
    w.second.save(update_fields=["name", "affiliation"])
    homonym = account("other-lin", teacher=True)
    homonym.researcher.name = "林岚"
    homonym.researcher.save(update_fields=["name"])
    AffiliationPeriod.objects.create(
        researcher=homonym.researcher, department=w.dept_a, starts_on=date(2020, 1, 1),
    )
    first_dismiss, second_dismiss = url("dismiss", w.first), url("dismiss", w.second)
    client = Client(enforce_csrf_checks=True)
    client.force_login(w.teacher)
    teacher_page = client.get(url("index")).content.decode()
    other_page = get_as(homonym, "index").content.decode()
    assert "标准化姓名一致" in teacher_page and "成果日期的院系一致：乙院" in teacher_page
    assert "other-lin" not in teacher_page and second_dismiss not in teacher_page
    assert first_dismiss in teacher_page and first_dismiss not in other_page
    assert second_dismiss in other_page
    assert post_as(homonym, "dismiss", w.first).status_code == 404
    assert client.get(first_dismiss).status_code == 405
    assert client.post(first_dismiss).status_code == 403
    assert not DismissedRecommendation.objects.exists()
    for _ in range(2):
        assert client.post(first_dismiss, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value).status_code == 302
    assert DismissedRecommendation.objects.get().researcher == w.teacher.researcher
    assert not ClaimCase.objects.exists() and not ClaimEvent.objects.exists()
    assert second_dismiss in get_as(homonym, "index").content.decode()
    page = client.get(url("index"), {"identifier": "10.5555/p1"}).content.decode()
    assert first_dismiss not in page and url("submit", w.first) in page
    assert post_as(w.teacher, "submit", w.first, evidence="主动认领").status_code == 302
    assert ClaimCase.objects.get().researcher == w.teacher.researcher
    assert ClaimEvent.objects.get().kind == Kind.SUBMITTED


def test_exclusive_affiliation_end_and_locked_review_route(casework, monkeypatch):
    w = casework
    today = timezone.localdate()
    AffiliationPeriod.objects.create(
        researcher=w.teacher.researcher, department=w.dept_a, starts_on=today - timedelta(days=40),
        ends_on=today,
    )
    case = submitted(w)
    assert case.review_department == w.dept_b
    AffiliationPeriod.objects.filter(researcher=w.teacher.researcher, department=w.dept_b).update(
        ends_on=today + timedelta(days=1),
    )
    AffiliationPeriod.objects.create(
        researcher=w.teacher.researcher, department=w.dept_a, starts_on=today + timedelta(days=1),
    )
    after_transfer = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: after_transfer)
    assert get_as(w.reviewer_a, "detail", case).status_code == 404
    case.refresh_from_db()
    assert case.review_department == w.dept_b
    assert post_as(w.reviewer_b, "review", case, decision="approve", reason="证据对应").status_code == 302
    assert Attribution.objects.get().researcher == w.teacher.researcher


@pytest.mark.parametrize("missing", ["no-primary", "overlap", "inactive-reviewer", "self-only"])
def test_fallback_to_school_without_eligible_department_reviewer(casework, missing):
    w = casework
    if missing == "no-primary":
        AffiliationPeriod.objects.update(is_primary=False)
    elif missing == "overlap":
        AffiliationPeriod.objects.create(
            researcher=w.teacher.researcher, department=w.dept_a, starts_on=timezone.localdate(),
        )
    else:
        w.reviewer_b.is_active = False
        w.reviewer_b.save(update_fields=["is_active"])
        if missing == "self-only":
            DepartmentReviewer.objects.create(user=w.teacher, department=w.dept_b)
    case = submitted(w)
    assert case.review_department_id is None
    assert "校级待分派" in case.events.get().reason
    assert post_as(w.teacher, "review", case, decision="approve", reason="自审").status_code == 403
    assert post_as(w.school, "review", case, decision="approve", reason="校级补位").status_code == 302


def test_independent_appeal_preserves_reasons_and_cannot_be_repeated(casework):
    w = casework
    # Being a school reviewer too does not let the original reviewer decide the appeal.
    w.reviewer_b.groups.set(w.school.groups.all())
    case = submitted(w)
    assert get_as(w.school, "review", case).status_code == 403
    assert post_as(w.reviewer_b, "review", case, decision="reject", reason=" ").status_code == 400
    assert post_as(w.reviewer_b, "review", case, decision="approve").status_code == 400
    assert post_as(w.reviewer_b, "review", case, decision="reject", reason="请补作品证明").status_code == 302
    assert post_as(w.teacher, "submit", w.first, evidence="再次开案").status_code == 409
    assert post_as(w.reviewer_b, "appeal", case, reason="冒名申诉").status_code == 403
    assert post_as(w.teacher, "appeal", case, reason=" ").status_code == 400
    assert post_as(w.teacher, "appeal", case, reason="补充作者档案").status_code == 302
    assert get_as(w.teacher, "appeal", case).status_code == 409
    assert post_as(w.reviewer_b, "resolve", case, decision="approve", reason="原审").status_code == 403
    assert post_as(w.school, "resolve", case, decision="approve", reason="档案已核实").status_code == 302
    assert post_as(w.teacher, "appeal", case, reason="重复申诉").status_code == 409
    case.refresh_from_db()
    assert (case.status, case.initial_reviewer) == (Status.APPROVED, w.reviewer_b)
    rows = list(case.events.values_list("kind", "actor_id", "reason"))
    assert rows[1:] == [
        (Kind.REJECTED, w.reviewer_b.pk, "请补作品证明"),
        (Kind.APPEALED, w.teacher.pk, "补充作者档案"),
        (Kind.OVERTURNED, w.school.pk, "档案已核实"),
    ]
    teacher_html = get_as(w.teacher, "detail", case).content.decode()
    assert "请补作品证明" in teacher_html and "档案已核实" in teacher_html
    assert "reviewer-b" not in teacher_html and "school-independent" not in teacher_html
    assert "school-independent" in get_as(w.reviewer_b, "detail", case).content.decode()


def test_school_fallback_review_cannot_resolve_its_own_rejection(casework):
    w = casework
    AffiliationPeriod.objects.all().delete()
    case = submitted(w)
    assert post_as(w.school, "review", case, decision="reject", reason="身份不明").status_code == 302
    assert post_as(w.teacher, "appeal", case, reason="补充单位").status_code == 302
    assert post_as(w.school, "resolve", case, decision="uphold", reason="仍不足").status_code == 403
    case.refresh_from_db()
    assert case.status == Status.APPEALED and not Attribution.objects.exists()
    alternate = account("alternate-reviewer", school=True)
    assert post_as(alternate, "resolve", case, decision="uphold", reason="仍无法对应").status_code == 302
    assert post_as(w.teacher, "submit", w.first, evidence="重复").status_code == 409
    assert post_as(w.teacher, "appeal", case, reason="重复").status_code == 409
    case.refresh_from_db()
    assert case.status == Status.UPHELD and not Attribution.objects.exists()


def test_competitor_waits_for_explicit_revocation_without_leaking_evidence(casework):
    w = casework
    other = account("competing-teacher", teacher=True, school=True)
    original = submitted(w, evidence="private/original")
    another_slot = submitted(w, slot=w.second)
    assert post_as(w.reviewer_b, "review", original, decision="approve", reason="原审核").status_code == 302
    assert post_as(w.reviewer_b, "review", another_slot, decision="approve", reason="另一个署名").status_code == 302
    competitor = submitted(w, actor=other, evidence="private/challenger")
    assert get_as(w.teacher, "detail", competitor).status_code == 404
    # Even an otherwise school-authorized user cannot inspect or revoke their competitor's case.
    assert get_as(other, "detail", original).status_code == 404
    assert post_as(other, "revoke", original, reason="利益冲突").status_code == 404
    assert "competing-teacher" not in get_as(w.teacher, "index").content.decode()
    approve = {"decision": "approve", "reason": "希望核准竞争申请"}
    conflict = post_as(w.school, "review", competitor, **approve)
    assert conflict.status_code == 409 and "private/original" not in conflict.content.decode()
    assert competitor.events.count() == 1
    assert post_as(w.school, "review", competitor, decision="conflict", reason="internal-conflict").status_code == 302
    assert "internal-conflict" not in get_as(other, "detail", competitor).content.decode()
    assert "internal-conflict" in get_as(w.school, "detail", competitor).content.decode()
    assert post_as(w.school, "review", competitor, decision="reject", reason="需更正位次").status_code == 302
    assert post_as(other, "appeal", competitor, reason="补充首位证明").status_code == 302
    alternate = account("second-school-reviewer", school=True)
    assert post_as(alternate, "resolve", competitor, **approve).status_code == 409
    previous = Attribution.objects.get(slot=w.first, valid_until__isnull=True)
    assert post_as(w.reviewer_b, "revoke", original, reason="院系无纠错权").status_code == 403
    assert post_as(alternate, "revoke", original, reason="原归属有误").status_code == 302
    previous.refresh_from_db()
    assert previous.valid_until == original.events.get(kind=Kind.REVOKED).occurred_at
    assert "原归属有误" in get_as(w.teacher, "detail", original).content.decode()
    assert post_as(w.teacher, "submit", w.first, evidence="撤销后不允许新案").status_code == 409
    assert post_as(alternate, "resolve", competitor, **approve).status_code == 302
    active = Attribution.objects.get(slot=w.first, valid_until__isnull=True)
    assert active.researcher_id == other.researcher.pk and active.valid_from >= previous.valid_until
    assert Attribution.objects.filter(valid_until__isnull=True).count() == 2
    assert original.events.filter(kind=Kind.APPROVED).exists()


def test_school_reassignment_is_explicit_and_audit_is_scoped(casework):
    w = casework
    case = submitted(w, evidence="restricted-evidence")
    admin = account("ordinary-admin", is_staff=True, is_superuser=True)
    for unauthorized in (admin, w.reviewer_a):
        assert get_as(unauthorized, "detail", case).status_code == 404
        assert post_as(unauthorized, "review", case, decision="approve", reason="越权").status_code == 404
        assert "restricted-evidence" not in get_as(unauthorized, "index").content.decode()
    assert post_as(w.reviewer_b, "reassign", case, department=w.dept_a.pk, reason="越权").status_code == 403
    assert post_as(w.school, "reassign", case, department=w.dept_b.pk, reason="相同").status_code == 409
    unstaffed = Department.objects.create(code="c", name="丙院")
    assert post_as(w.school, "reassign", case, department=unstaffed.pk, reason="无合格审核人").status_code == 409
    assert post_as(w.school, "reassign", case, department=w.dept_a.pk, reason=" ").status_code == 400
    assert post_as(w.school, "reassign", case, department=w.dept_a.pk, reason="private-route").status_code == 302
    event = case.events.get(kind=Kind.REASSIGNED)
    assert (event.from_department, event.to_department, event.actor) == (w.dept_b, w.dept_a, w.school)
    assert get_as(w.reviewer_b, "detail", case).status_code == 404
    assert url("detail", case) not in get_as(w.reviewer_b, "index").content.decode()
    assert "private-route" in get_as(w.reviewer_a, "detail", case).content.decode()
    assert "private-route" not in get_as(w.teacher, "detail", case).content.decode()
    assert post_as(w.school, "reassign", case, department="", reason="校级补位").status_code == 302
    assert post_as(w.school, "review", case, decision="approve", reason="证据充分").status_code == 302
    assert post_as(w.school, "reassign", case, department=w.dept_a.pk, reason="已终结").status_code == 409


def test_db_enforces_unique_active_attribution_and_append_only_audit(casework):
    w = casework
    case = submitted(w)
    services.review_claim(actor=w.reviewer_b, case_id=case.pk, decision="approve", reason="属实")
    other = account("other", teacher=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        Attribution.objects.create(slot=w.first, researcher=other.researcher)
    with pytest.raises(IntegrityError), transaction.atomic():
        ClaimCase.objects.create(slot=w.first, researcher=w.teacher.researcher, evidence="duplicate")
    if connection.vendor == "postgresql":
        with pytest.raises(IntegrityError), transaction.atomic():
            case.events.update(reason="rewritten")
        with pytest.raises(IntegrityError), transaction.atomic():
            case.events.all().delete()
    assert case.events.filter(kind=Kind.APPROVED, reason="属实").exists()


def test_audit_failure_rolls_back_attribution_and_decision(casework, monkeypatch):
    w = casework
    case = submitted(w)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated audit write failure")

    monkeypatch.setattr(services.ClaimEvent.objects, "create", fail)
    with pytest.raises(RuntimeError, match="simulated audit write failure"):
        services.review_claim(actor=w.reviewer_b, case_id=case.pk, decision="approve", reason="属实")
    case.refresh_from_db()
    assert case.status == Status.PENDING and case.initial_reviewer is None
    assert case.events.count() == 1 and not Attribution.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["approve", "submit"])
def test_concurrent_writers_serialize_on_postgresql_slot(casework, operation):
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locking")
    w = casework
    pids = Queue()
    if operation == "approve":
        # Both decisions have legitimate reviewers; the cases share exactly one slot.
        first_case = submitted(w)
        second_case = submitted(w, actor=account("competing", teacher=True))
        requests = [(w.reviewer_b.pk, first_case.pk), (w.school.pk, second_case.pk)]
    else:
        requests = [(w.teacher.pk, w.first.pk)] * 2

    def writer(user_id, target_id):
        conn = connections["default"]
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET lock_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids.put(cursor.fetchone()[0])
            actor = get_user_model().objects.get(pk=user_id)
            if operation == "approve":
                services.review_claim(actor=actor, case_id=target_id, decision="approve", reason="属实")
            else:
                services.submit_claim(actor=actor, slot_id=target_id, evidence="本人署名")
            return "success"
        except services.WorkflowConflict:
            return "conflict"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic():
            ContributionSlot.objects.select_for_update().get(pk=w.first.pk)
            futures = [pool.submit(writer, *args) for args in requests]
            worker_pids = [pids.get(timeout=5) for _ in futures]
            deadline = monotonic() + 8
            with connection.cursor() as cursor:
                while True:
                    cursor.execute(
                        "SELECT pg_blocking_pids(%s), pg_blocking_pids(%s)", worker_pids,
                    )
                    if all(cursor.fetchone()):
                        break
                    if monotonic() >= deadline:
                        pytest.fail("both writers did not contend on the locked slot")
                    sleep(0.02)
        assert sorted(future.result(timeout=10) for future in futures) == ["conflict", "success"]

    if operation == "approve":
        assert Attribution.objects.filter(slot=w.first, valid_until__isnull=True).count() == 1
        assert ClaimCase.objects.filter(slot=w.first, status=Status.APPROVED).count() == 1
        assert ClaimCase.objects.filter(slot=w.first, status=Status.PENDING).count() == 1
        assert ClaimEvent.objects.filter(kind=Kind.APPROVED).count() == 1
    else:
        assert ClaimCase.objects.filter(slot=w.first).count() == 1
        assert ClaimEvent.objects.filter(kind=Kind.SUBMITTED).count() == 1
