import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import ContributionSlot, Work
from apps.claims.models import Attribution, ClaimCase
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher
from apps.reports import services
from apps.reports.models import ReportSnapshot
from apps.reports.services import ReportFilters, freeze_snapshot, snapshot_stats, stats

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def uncollected_static_files(settings, tmp_path):
    settings.STATIC_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def demo(django_user_model):
    def user(username):
        return django_user_model.objects.create_user(username=username)

    alpha = Department.objects.create(code="alpha", name="甲院")
    beta = Department.objects.create(code="beta", name="乙院")
    gamma = Department.objects.create(code="gamma", name="丙院")
    school = user("school")
    school.groups.add(Group.objects.create(name="school-reviewers"))
    a_reviewer, b_reviewer, g_reviewer = (user(name) for name in ("a", "b", "g"))
    for reviewer, department in zip((a_reviewer, b_reviewer, g_reviewer), (alpha, beta, gamma)):
        DepartmentReviewer.objects.create(user=reviewer, department=department)
    lin = Researcher.objects.create(user=user("lin"), name="林岚")
    chen = Researcher.objects.create(user=user("chen"), name="陈星")
    AffiliationPeriod.objects.create(
        researcher=lin,
        department=alpha,
        starts_on=date(2023, 1, 1),
        ends_on=date(2026, 1, 1),
    )
    AffiliationPeriod.objects.create(researcher=lin, department=beta, starts_on=date(2026, 1, 1))
    AffiliationPeriod.objects.create(researcher=chen, department=beta, starts_on=date(2023, 1, 1))
    paper = Work.objects.create(
        kind="paper",
        identifier="10.0000/p1",
        title="P1 论文",
        published_on=date(2025, 10, 10),
    )
    p1_lin = _slot(paper, 1, "林岚")
    p1_chen = _slot(paper, 2, "陈星")
    return SimpleNamespace(
        alpha=alpha,
        beta=beta,
        gamma=gamma,
        school=school,
        a_reviewer=a_reviewer,
        b_reviewer=b_reviewer,
        g_reviewer=g_reviewer,
        lin=lin,
        chen=chen,
        paper=paper,
        p1_lin=p1_lin,
        p1_chen=p1_chen,
    )


def _slot(work, position, name):
    return ContributionSlot.objects.create(
        work=work,
        position=position,
        name=name,
        source_slot_id=f"{position}-{name}",
    )


def _approve(slot, researcher):
    return Attribution.objects.create(
        slot=slot,
        researcher=researcher,
        valid_from=timezone.now() - timedelta(days=1),
    )


def _counts(report):
    return report.school_count, {d["code"]: d["count"] for d in report.departments}


def _csv(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert "no-store" in response["Cache-Control"]
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"), newline="")))


def _route(name, snapshot_id=None):
    return reverse(f"reports:{name}", args=[snapshot_id] if snapshot_id is not None else [])


def test_biz_05_golden_totals_and_dashboard_drilldown_csv_agree(client, demo):
    # The second author has an earlier approval; this cannot satisfy strict ordering.
    second = _approve(demo.p1_chen, demo.chen)
    second.valid_from -= timedelta(days=1)
    second.save(update_fields=["valid_from"])
    _approve(demo.p1_lin, demo.lin)
    patent = Work.objects.create(
        kind="patent",
        identifier="T1",
        title="T1 专利",
        published_on=date(2025, 12, 1),
        granted_on=date(2026, 7, 1),
    )
    _approve(_slot(patent, 1, "林岚"), demo.lin)

    client.force_login(demo.school)
    cases = [
        (2025, "overview", 2, 2, 1, {demo.paper.pk: "2025-10-10", patent.pk: "2025-12-01"}),
        (2025, "strict", 1, 1, 0, {demo.paper.pk: "2025-10-10"}),
        (2026, "strict", 1, 0, 1, {patent.pk: "2026-07-01"}),
    ]
    for year, policy, total, alpha, beta, events in cases:
        query = {"year": year, "policy": policy}
        dashboard = client.get(_route("index"), query)
        assert dashboard.status_code == 200
        assert _counts(dashboard.context["report"]) == (
            total,
            {"alpha": alpha, "beta": beta, "gamma": 0},
        )
        assert f"data-report-total>{total}<" in dashboard.content.decode()
        assert "模拟源 / 合成数据" in dashboard.content.decode()
        for dept, count in ((None, total), (demo.alpha, alpha), (demo.beta, beta)):
            scoped = {**query, **({"department": dept.pk} if dept else {})}
            details = client.get(_route("details"), scoped)
            csv_rows = _csv(client.get(_route("csv"), scoped))
            assert details.status_code == 200
            rows = details.context["report"].rows
            assert {int(row["work_id"]): row["event_date"] for row in csv_rows} == {
                row["work_id"]: row["event_date"] for row in rows
            }
            assert sum(int(row["counted"]) for row in csv_rows) == count
            assert {int(row["work_id"]): row["counted"] == "1" for row in csv_rows} == {
                row["work_id"]: row["counted"] for row in rows
            }
            if dept is None:
                assert {int(row["work_id"]): row["event_date"] for row in csv_rows} == events
    strict_beta = client.get(
        _route("details"), {"year": 2025, "policy": "strict", "department": demo.beta.pk}
    )
    assert "不是来源首位署名" in strict_beta.content.decode()


def test_biz_04_snapshot_stays_unchanged_after_approvals_and_source_edits(client, demo):
    chen = _approve(demo.p1_chen, demo.chen)
    client.force_login(demo.school)
    query = {"year": 2025, "work_id": demo.paper.pk}
    assert client.get(_route("freeze")).status_code == 405
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(demo.school)
    assert csrf_client.post(_route("freeze"), query).status_code == 403
    assert not ReportSnapshot.objects.exists()
    frozen = {}
    for policy in ("overview", "strict"):
        response = client.post(_route("freeze"), {**query, "policy": policy})
        assert response.status_code == 302
        item = ReportSnapshot.objects.get(policy=policy)
        assert response.url == _route("snapshot", item.pk)
        assert item.filters == {**query, "policy": policy, "kind": ""}
        assert item.created_by_id == demo.school.pk
        assert item.implementation_version == "reports-v1"
        report = snapshot_stats(demo.school, item.pk)
        frozen[policy] = (item.pk, report, client.get(_route("snapshot_csv", item.pk)).content)
    assert _counts(frozen["overview"][1]) == (1, {"alpha": 0, "beta": 1, "gamma": 0})
    assert _counts(frozen["strict"][1]) == (0, {"alpha": 0, "beta": 0, "gamma": 0})

    _approve(demo.p1_lin, demo.lin)
    assert _counts(stats(demo.school, ReportFilters(**query, policy="strict"))) == (
        1,
        {"alpha": 1, "beta": 0, "gamma": 0},
    )
    chen.valid_until = timezone.now()
    chen.save(update_fields=["valid_until"])
    live = stats(demo.school, ReportFilters(**query))
    assert _counts(live) == (1, {"alpha": 1, "beta": 0, "gamma": 0})
    assert live.rows[0]["facts"][1]["reasons"][0]["code"] == "revoked"

    Work.objects.filter(pk=demo.paper.pk).update(title="来源更正", published_on=date(2026, 2, 1))
    Department.objects.filter(pk=demo.beta.pk).update(name="院系更名", code="beta-renamed")
    Researcher.objects.filter(pk=demo.chen.pk).update(name="人员更名")
    ContributionSlot.objects.filter(pk=demo.p1_chen.pk).update(name="署名更正", position=3)
    assert stats(demo.school, ReportFilters(**query)).count == 0
    for snapshot_id, old_report, old_csv in frozen.values():
        assert snapshot_stats(demo.school, snapshot_id) == old_report
        # Snapshot URLs do not let query parameters recalculate frozen filters.
        page = client.get(_route("snapshot_details", snapshot_id), {"year": 2026})
        assert page.status_code == 200
        assert page.context["report"] == old_report
        assert client.get(_route("snapshot_csv", snapshot_id)).content == old_csv


def test_biz_06_affiliation_conflicts_are_not_guessed(client, demo):
    _approve(demo.p1_lin, demo.lin)
    _approve(demo.p1_chen, demo.chen)
    AffiliationPeriod.objects.create(
        researcher=demo.lin, department=demo.beta, starts_on=demo.paper.published_on
    )
    AffiliationPeriod.objects.filter(researcher=demo.chen).update(is_primary=False)
    client.force_login(demo.school)
    query = {"year": 2025}
    response = client.get(_route("details"), query)
    assert response.status_code == 200
    assert response.context["report"].count == 0
    assert "事件日主归属重叠，待核对" in response.content.decode()
    assert "事件日无主归属院系，待核对" in response.content.decode()
    row = _csv(client.get(_route("csv"), query))[0]
    assert row["counted"] == "0"
    assert row["department_codes"] == ""
    assert "事件日主归属重叠" in row["explanations"]
    client.force_login(demo.b_reviewer)
    assert _csv(client.get(_route("csv"), query)) == []


def test_biz_06_permissions_checked_at_every_read_and_freeze(client, demo, django_user_model):
    _approve(demo.p1_lin, demo.lin)
    ClaimCase.objects.create(
        slot=demo.p1_lin,
        researcher=demo.lin,
        review_department=demo.beta,
        evidence="PRIVATE-EVIDENCE-SENTINEL",
    )
    query = {"year": 2025}
    snapshot_id = freeze_snapshot(demo.school, ReportFilters(**query))
    target_id = freeze_snapshot(demo.school, ReportFilters(**query, work_id=demo.paper.pk))
    live_routes = [_route(name) for name in ("index", "details", "csv")]
    frozen_routes = [
        _route(name, snapshot_id)
        for name in (
            "snapshot",
            "snapshot_details",
            "snapshot_csv",
        )
    ]
    assert all(client.get(path).status_code == 302 for path in live_routes + frozen_routes)

    admin = django_user_model.objects.create_user(
        username="admin", is_staff=True, is_superuser=True
    )
    for user in (admin, demo.lin.user):
        client.force_login(user)
        assert all(
            client.get(path, query).status_code == 403 for path in live_routes + frozen_routes
        )
        assert client.post(_route("freeze"), query).status_code == 403

    client.force_login(demo.g_reviewer)
    for path in live_routes + frozen_routes:
        response = client.get(path, query)
        assert response.status_code == 200
        assert demo.paper.identifier not in response.content.decode()
        assert str(target_id) not in response.content.decode()
        assert client.get(path, {**query, "department": demo.alpha.pk}).status_code == 403
    for name in ("snapshot", "snapshot_details", "snapshot_csv"):
        assert client.get(_route(name, target_id)).status_code == 404
    assert client.post(_route("freeze"), query).status_code == 403

    client.force_login(demo.a_reviewer)
    for path in (
        live_routes[1],
        live_routes[2],
        *frozen_routes[1:],
        _route("snapshot_csv", target_id),
    ):
        response = client.get(path, query)
        assert response.status_code == 200
        assert demo.paper.identifier in response.content.decode()
        assert "PRIVATE-EVIDENCE-SENTINEL" not in response.content.decode()
        assert demo.beta.name not in response.content.decode()
    DepartmentReviewer.objects.filter(user=demo.a_reviewer).update(department=demo.beta)
    assert _csv(client.get(frozen_routes[2])) == []
    assert client.get(_route("snapshot_csv", target_id)).status_code == 404
    DepartmentReviewer.objects.filter(user=demo.a_reviewer).delete()
    assert client.get(frozen_routes[2]).status_code == 403

    client.force_login(demo.school)
    demo.school.groups.clear()
    assert client.get(frozen_routes[2]).status_code == 403
    assert client.post(_route("freeze"), query).status_code == 403


def test_dates_reliable_first_and_affiliation_boundaries(demo):
    _approve(demo.p1_lin, demo.lin)
    _slot(demo.paper, 1, "Ambiguous position")
    assert stats(demo.school, ReportFilters(2025, "strict", "paper")).count == 0
    early_grant = Work.objects.create(
        kind="patent",
        title="grant first",
        identifier="T-grant",
        published_on=date(2026, 8, 1),
        granted_on=date(2026, 1, 1),
    )
    grant_only = Work.objects.create(
        kind="patent",
        title="grant only",
        identifier="T-only",
        granted_on=date(2026, 9, 1),
    )
    public_only = Work.objects.create(
        kind="patent",
        title="not granted",
        identifier="T-open",
        published_on=date(2026, 9, 1),
    )
    no_date = Work.objects.create(
        kind="paper", title="undated", identifier="P-no-date", granted_on=date(2026, 1, 1)
    )
    for work in (early_grant, grant_only, public_only, no_date):
        _approve(_slot(work, 1, "林岚"), demo.lin)
    overview = stats(demo.school, ReportFilters(2026, "overview", "patent"))
    strict = stats(demo.school, ReportFilters(2026, "strict", "patent"))
    assert _counts(overview) == (3, {"alpha": 0, "beta": 3, "gamma": 0})
    assert _counts(strict) == (2, {"alpha": 0, "beta": 2, "gamma": 0})
    assert overview.rows[0]["event_date"] == "2026-01-01"
    assert strict.rows[-1]["facts"][0]["reasons"][0]["code"] == "missing_grant"
    for policy in ("overview", "strict"):
        paper = stats(demo.school, ReportFilters(2026, policy, "paper"))
        assert paper.count == 0
        assert paper.rows[0]["facts"][0]["reasons"][0]["code"] == "missing_publication"


def test_distinct_counts_with_same_department_and_future_approval(demo, django_user_model):
    _approve(demo.p1_lin, demo.lin)
    _approve(demo.p1_chen, demo.chen)
    same_department = Researcher.objects.create(
        user=django_user_model.objects.create_user(username="additional-coauthor"),
        name="王宁",
    )
    AffiliationPeriod.objects.create(
        researcher=same_department,
        department=demo.alpha,
        starts_on=demo.paper.published_on,
    )
    _approve(_slot(demo.paper, 3, "王宁"), same_department)
    future = Work.objects.create(
        kind="paper",
        title="future approval",
        identifier="P-future",
        published_on=demo.paper.published_on,
    )
    Attribution.objects.create(
        slot=_slot(future, 1, "林岚"),
        researcher=demo.lin,
        valid_from=timezone.now() + timedelta(days=1),
    )
    report = stats(demo.school, ReportFilters(2025))
    assert _counts(report) == (1, {"alpha": 1, "beta": 1, "gamma": 0})
    assert report.rows[1]["facts"][0]["reasons"][0]["code"] == "not_approved"
    DepartmentReviewer.objects.create(user=demo.a_reviewer, department=demo.beta)
    scoped = stats(demo.a_reviewer, ReportFilters(2025))
    assert scoped.count == 1
    assert scoped.school_count is None
    assert {d["code"]: d["count"] for d in scoped.departments} == {"alpha": 1, "beta": 1}


def test_csv_escaping_html_and_bad_filters(client, demo):
    title = '=HYPERLINK("https://example.com", "<script>危险</script>")\n下一行'
    identifier = "  ＋1,2"
    Work.objects.filter(pk=demo.paper.pk).update(title=title, identifier=identifier)
    _approve(demo.p1_lin, demo.lin)
    client.force_login(demo.school)
    query = {"year": 2025}
    html = client.get(_route("details"), query).content.decode()
    assert "<script>危险</script>" not in html
    assert "&lt;script&gt;危险&lt;/script&gt;" in html
    rows = _csv(client.get(_route("csv"), query))
    assert len(rows) == 1
    assert rows[0]["title"] == "'" + title
    assert rows[0]["identifier"] == "'" + identifier
    for invalid in ({"year": 10000}, {"policy": "unknown"}, {"department": -1}, {"kind": "x"}):
        assert client.get(_route("csv"), invalid).status_code == 400


def test_postgresql_rejects_snapshot_update_and_delete(demo):
    snapshot_id = freeze_snapshot(demo.school, ReportFilters(2025))
    row = ReportSnapshot.objects.filter(pk=snapshot_id)
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        row.update(payload={})
    with pytest.raises(IntegrityError, match="immutable"), transaction.atomic():
        row.delete()
    assert row.exists()


def test_freeze_reads_one_postgresql_snapshot_during_concurrent_edit(demo, monkeypatch):
    _approve(demo.p1_lin, demo.lin)
    real_affiliations = services._affiliations

    def edit_catalog_and_roster():
        try:
            with transaction.atomic():
                Work.objects.filter(pk=demo.paper.pk).update(title="Concurrent title")
                AffiliationPeriod.objects.filter(researcher=demo.lin, department=demo.alpha).update(
                    department=demo.gamma,
                )
        finally:
            connections.close_all()

    def edit_between_fact_queries(attributions, event_dates):
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            assert cursor.fetchone() == ("repeatable read",)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(edit_catalog_and_roster).result(timeout=10)
        return real_affiliations(attributions, event_dates)

    filters = ReportFilters(2025, work_id=demo.paper.pk)
    with monkeypatch.context() as patch:
        patch.setattr(services, "_affiliations", edit_between_fact_queries)
        snapshot_id = freeze_snapshot(demo.school, filters)
    frozen = snapshot_stats(demo.school, snapshot_id)
    live = stats(demo.school, filters)
    assert _counts(frozen) == (1, {"alpha": 1, "beta": 0, "gamma": 0})
    assert _counts(live) == (1, {"alpha": 0, "beta": 0, "gamma": 1})
    assert frozen.rows[0]["title"] == demo.paper.title
    assert live.rows[0]["title"] == "Concurrent title"
