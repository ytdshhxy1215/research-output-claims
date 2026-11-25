import csv
import io
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import ContributionSlot, Work
from apps.claims.services import review_claim, submit_claim
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher

pytestmark = pytest.mark.django_db

HOME = reverse("home")
CLAIMS = reverse("claims:index")
REPORTS = reverse("reports:index")
SYNC = reverse("sync_operations")
LOGIN = reverse("login")
LOGOUT = reverse("logout")
REPORT_ROUTES = (REPORTS, *(reverse(f"reports:{name}") for name in ("details", "csv", "trend")))
NO_REPORT_ROLE = "当前账号没有成果统计权限"
OUTSIDE_SCOPE = "这项统计操作超出当前授权范围"
EMPTY_RESULT = "当前筛选下暂无可计入成果。"
DEMO_LINK = "查看 2025 演示年度"


class Page(HTMLParser):
    """Read rendered HTML, not template context or server-side role flags."""

    def __init__(self, response):
        super().__init__()
        self.links = []
        self.forms = []
        self.region = None
        self.link = None
        self.form = None
        self.feed(response.content.decode(response.charset))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in {"nav", "main"}:
            self.region = tag
        elif tag == "a" and attrs.get("href"):
            self.link = SimpleNamespace(url=attrs["href"], region=self.region, text="")
            self.links.append(self.link)
        elif tag == "form":
            self.form = SimpleNamespace(attrs=attrs, region=self.region, fields={}, text="")
            self.forms.append(self.form)
        elif tag == "input" and self.form and attrs.get("type") == "hidden":
            self.form.fields[attrs.get("name")] = attrs.get("value")

    def handle_endtag(self, tag):
        if tag in {"nav", "main"}:
            self.region = None
        elif tag == "a":
            self.link = None
        elif tag == "form":
            self.form = None

    def handle_data(self, text):
        for element in (self.link, self.form):
            if element:
                element.text += text

    def business_paths(self, *, region=None):
        paths = {
            urlsplit(link.url).path for link in self.links if region is None or link.region == region
        }
        return paths & {CLAIMS, REPORTS, SYNC}


def _nav(client, path, expected):
    response = client.get(path)
    assert response.status_code == 200
    page = Page(response)
    assert page.business_paths(region="nav") == expected
    assert page.business_paths() == expected


@pytest.fixture
def workspace(django_user_model):
    def user(name):
        return django_user_model.objects.create_user(username=f"workspace-{name}")

    alpha = Department.objects.create(code="workspace-alpha", name="合成甲院")
    beta = Department.objects.create(
        pk=2_093_716_521, code="workspace-private-beta", name="不应披露的合成乙院",
    )
    users = {name: user(name) for name in ("teacher", "department", "school", "ops", "none", "beta")}
    beta_reviewer = user("beta-reviewer")
    DepartmentReviewer.objects.create(user=users["department"], department=alpha)
    DepartmentReviewer.objects.create(user=beta_reviewer, department=beta)
    school_role = Group.objects.create(name="school-reviewers")
    users["school"].groups.add(school_role)
    sync_role = Group.objects.create(name="source-operators")
    sync_role.permissions.add(
        Permission.objects.get(content_type__app_label="sources", codename="view_syncrun")
    )
    users["ops"].groups.add(sync_role)

    def approve_paper(author, department, reviewer, name):
        researcher = Researcher.objects.create(user=author, name=f"合成作者 {name}")
        AffiliationPeriod.objects.create(
            researcher=researcher, department=department, starts_on=date(2024, 1, 1),
        )
        paper = Work.objects.create(
            kind="paper", identifier=f"10.0000/workspace-{name}",
            title=f"合成论文 {name}", published_on=date(2025, 3, 1),
        )
        slot = ContributionSlot.objects.create(
            work=paper, position=1, name=researcher.name, source_slot_id=f"workspace-{name}-1",
        )
        case = submit_claim(actor=author, slot_id=slot.pk, evidence="合成验收证据")
        review_claim(actor=reviewer, case_id=case.pk, decision="approve", reason="合成初审结果")
        return paper, case

    alpha_paper, alpha_case = approve_paper(users["teacher"], alpha, users["department"], "alpha")
    beta_paper, _ = approve_paper(users["beta"], beta, beta_reviewer, "private-beta")
    return SimpleNamespace(
        users=users, alpha=alpha, beta=beta, alpha_paper=alpha_paper, beta_paper=beta_paper,
        alpha_case=alpha_case, school_role=school_role, sync_role=sync_role,
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("teacher", {CLAIMS}),
        ("department", {CLAIMS, REPORTS}),
        ("school", {CLAIMS, REPORTS}),
        ("ops", {SYNC}),
        ("none", set()),
    ],
)
def test_home_and_workspace_navigation_match_actual_roles(client, workspace, role, expected):
    client.force_login(workspace.users[role])
    _nav(client, HOME, expected)
    entry = CLAIMS if CLAIMS in expected else SYNC if SYNC in expected else None
    if entry:
        _nav(client, entry, expected)


@pytest.mark.parametrize("route", REPORT_ROUTES)
def test_anonymous_report_url_redirects_to_login(client, route):
    response = client.get(route, {"year": 2025})
    assert response.status_code == 302
    target = urlsplit(response.url)
    assert target.path == LOGIN and not target.netloc
    assert parse_qs(target.query) == {"next": [f"{route}?year=2025"]}


@pytest.mark.parametrize("route", REPORT_ROUTES)
def test_teacher_direct_report_url_is_friendly_403_not_a_data_response(client, workspace, route):
    client.force_login(workspace.users["teacher"])
    response = client.get(
        route, {"year": 2025, "access": "school", "department": workspace.alpha.pk},
        headers={"Access": "school", "X-Workspace-Role": "school-reviewers"},
    )
    assert response.status_code == 403
    assert response["Content-Type"].startswith("text/html")
    assert "Content-Disposition" not in response
    body = response.content.decode(response.charset)
    assert NO_REPORT_ROLE in body
    assert workspace.alpha_paper.identifier not in body
    page = Page(response)
    assert page.business_paths() == {CLAIMS}
    assert any(
        link.region == "main" and link.url == CLAIMS and "返回认领工作台" in link.text
        for link in page.links
    )


def test_forbidden_page_switch_account_posts_logout_with_csrf(workspace):
    client = Client(enforce_csrf_checks=True)
    client.force_login(workspace.users["teacher"])
    forbidden = client.get(REPORTS)
    assert forbidden.status_code == 403
    form, = [
        item for item in Page(forbidden).forms
        if item.region == "main" and item.attrs.get("action") == LOGOUT
    ]
    assert form.attrs.get("method", "").lower() == "post"
    assert "切换账号" in form.text
    assert form.fields.get("next") == LOGIN
    assert form.fields.get("csrfmiddlewaretoken")
    assert client.get(LOGOUT).status_code == 405
    assert client.post(LOGOUT, {"next": LOGIN}).status_code == 403
    assert client.get(CLAIMS).status_code == 200
    switched = client.post(LOGOUT, form.fields)
    assert switched.status_code == 302 and switched.url == LOGIN
    assert client.get(LOGIN).status_code == 200
    assert client.get(CLAIMS).status_code == 302
    assert client.get(REPORTS).status_code == 302


def test_report_scope_is_enforced_and_denial_does_not_disclose_target(client, workspace):
    client.force_login(workspace.users["department"])
    query = {"year": 2025, "policy": "strict"}
    missing_department = 8_726_491_830
    for route in REPORT_ROUTES:
        for requested in (None, workspace.alpha.pk):
            params = query if requested is None else {**query, "department": requested}
            response = client.get(route, params)
            assert response.status_code == 200
            assert workspace.beta.name not in response.content.decode(response.charset)
        for requested in (workspace.beta.pk, missing_department):
            denied = client.get(route, {**query, "department": requested})
            assert denied.status_code == 403 and "Content-Disposition" not in denied
            body = denied.content.decode(denied.charset)
            assert OUTSIDE_SCOPE in body
            for sentinel in (
                workspace.beta.name, workspace.beta.code, workspace.beta_paper.identifier,
                workspace.beta_paper.title,
            ):
                assert sentinel not in body
            assert f"department={requested}" not in body
            assert not any(
                form.fields.get("department") == str(requested) for form in Page(denied).forms
            )
            if requested == missing_department:
                assert str(missing_department) not in body

    def exported_ids(department=None):
        params = query if department is None else {**query, "department": department}
        response = client.get(reverse("reports:csv"), params)
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        return {
            row["identifier"] for row in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
        }

    assert exported_ids() == {workspace.alpha_paper.identifier}
    client.force_login(workspace.users["school"])
    assert client.get(REPORTS, {**query, "department": workspace.beta.pk}).status_code == 200
    assert exported_ids(workspace.beta.pk) == {workspace.beta_paper.identifier}
    assert exported_ids() == {workspace.alpha_paper.identifier, workspace.beta_paper.identifier}


def test_mixed_roles_add_entry_points_without_broadening_object_access(client, workspace):
    teacher = workspace.users["teacher"]
    teacher.groups.add(workspace.sync_role)
    DepartmentReviewer.objects.create(user=teacher, department=workspace.alpha)
    competitor = submit_claim(
        actor=workspace.users["beta"], slot_id=workspace.alpha_case.slot_id,
        evidence="合成竞争认领，不应向同署名申请人披露",
    )
    client.force_login(teacher)
    _nav(client, HOME, {CLAIMS, REPORTS, SYNC})
    assert client.get(REPORTS, {"department": workspace.alpha.pk}).status_code == 200
    assert client.get(REPORTS, {"department": workspace.beta.pk}).status_code == 403
    teacher.groups.add(workspace.school_role)
    _nav(client, HOME, {CLAIMS, REPORTS, SYNC})
    assert client.get(REPORTS, {"department": workspace.beta.pk}).status_code == 200
    assert client.get(reverse("claims:detail", args=[workspace.alpha_case.pk])).status_code == 200
    assert client.get(reverse("claims:detail", args=[competitor.pk])).status_code == 404


@pytest.mark.parametrize("role", ["department", "school"])
def test_revocation_updates_navigation_and_direct_permissions_in_same_session(client, workspace, role):
    reviewer = workspace.users[role]
    client.force_login(reviewer)
    _nav(client, HOME, {CLAIMS, REPORTS})
    assert client.get(REPORTS).status_code == 200
    if role == "department":
        DepartmentReviewer.objects.filter(user=reviewer).delete()
    else:
        reviewer.groups.remove(workspace.school_role)
    _nav(client, HOME, set())
    for route in REPORT_ROUTES:
        assert client.get(route).status_code == 403


def test_empty_live_report_defaults_to_current_year_and_demo_link_keeps_filters(client, workspace):
    client.force_login(workspace.users["department"])
    current_year = timezone.localdate().year
    default = client.get(REPORTS)
    assert default.status_code == 200
    assert f"事件日期：{current_year}-01-01 至 {current_year}-12-31" in default.content.decode()
    if current_year != 2025:
        assert EMPTY_RESULT in default.content.decode()
        assert any(DEMO_LINK in link.text for link in Page(default).links)

    filters = {"policy": "strict", "kind": "paper", "department": workspace.alpha.pk}
    empty = client.get(REPORTS, {**filters, "year": 2024})
    assert empty.status_code == 200 and EMPTY_RESULT in empty.content.decode()
    demo, = [link for link in Page(empty).links if DEMO_LINK in link.text]
    assert demo.region == "main"
    target = urlsplit(demo.url)
    assert target.path == REPORTS and not target.netloc and not target.scheme
    assert parse_qs(target.query) == {
        key: [str(value)] for key, value in {**filters, "year": 2025}.items()
    }
    result = client.get(demo.url)
    assert result.status_code == 200
    assert EMPTY_RESULT not in result.content.decode()
    assert not any(DEMO_LINK in link.text for link in Page(result).links)
    assert workspace.beta.name not in result.content.decode()
    assert 'data-department="workspace-alpha"' in result.content.decode()


@pytest.mark.django_db(transaction=True)
def test_empty_frozen_report_does_not_offer_live_demo_hint(client, workspace):
    client.force_login(workspace.users["school"])
    query = {"year": 2024, "policy": "strict", "kind": "paper"}
    live = client.get(REPORTS, query)
    assert live.status_code == 200 and EMPTY_RESULT in live.content.decode()
    assert any(DEMO_LINK in link.text for link in Page(live).links)
    freeze = client.post(reverse("reports:freeze"), query)
    assert freeze.status_code == 302
    for path in (freeze.url, f"{freeze.url}details/"):
        frozen = client.get(path)
        assert frozen.status_code == 200
        body = frozen.content.decode(frozen.charset)
        assert "冻结快照" in body
        assert EMPTY_RESULT not in body and DEMO_LINK not in body
        assert not any(
            urlsplit(link.url).path == REPORTS
            and parse_qs(urlsplit(link.url).query).get("year") == ["2025"]
            for link in Page(frozen).links
        )
