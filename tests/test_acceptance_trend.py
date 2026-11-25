"""Independent acceptance of the cross-year reports HTTP contract."""

import re
from datetime import date
from html import unescape
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group

from apps.catalog.models import ContributionSlot, Work
from apps.claims.models import Attribution
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher

pytestmark = pytest.mark.django_db(transaction=True)
TREND = "/reports/trend/"
DETAILS = "/reports/details/"


@pytest.fixture
def trend_case(django_user_model):
    def user(name):
        return django_user_model.objects.create_user(username=name)

    alpha = Department.objects.create(code="ac-alpha", name="Trend Alpha")
    beta = Department.objects.create(code="ac-beta", name="Trend Beta")
    school = user("trend-school")
    school.groups.add(Group.objects.create(name="school-reviewers"))
    reviewers = {dept.pk: user(f"review-{dept.code}") for dept in (alpha, beta)}
    dual = user("trend-dual")
    for dept in (alpha, beta):
        for reviewer in (reviewers[dept.pk], dual):
            DepartmentReviewer.objects.create(user=reviewer, department=dept)

    def researcher(name, dept):
        person = Researcher.objects.create(user=user(name), name=name)
        AffiliationPeriod.objects.create(researcher=person, department=dept, starts_on=date(2022, 1, 1))
        return person

    a = researcher("Trend-A-approved", alpha)
    b = researcher("Trend-B-private", beta)

    def work(key, kind="paper", published=None, granted=None, slots=()):
        item = Work.objects.create(
            kind=kind, identifier=key, title=key, published_on=published, granted_on=granted,
        )
        for position, (person, approved) in enumerate(slots, 1):
            slot = ContributionSlot.objects.create(
                work=item, position=position, name=person.name, source_slot_id=f"slot-{position}",
            )
            if approved:
                Attribution.objects.create(slot=slot, researcher=person)
        return item

    work("10.7777/ac-outside", published=date(2023, 1, 1), slots=((a, True),))
    shared = work("10.7777/ac-shared", published=date(2024, 9, 1), slots=((a, True), (b, True)))
    patent = work("AC-TREND-PATENT", "patent", date(2024, 3, 1), date(2026, 2, 1), ((a, True),))
    beta_only = work(
        "10.7777/ac-beta-only", published=date(2026, 1, 1), slots=((a, False), (b, True)),
    )
    pending = work("10.7777/ac-pending", published=date(2025, 1, 1), slots=((a, False),))
    work("10.7777/ac-undated", slots=((a, True),))
    return SimpleNamespace(
        school=school, alpha=alpha, beta=beta, a=a, b=b, dual=dual, shared=shared, patent=patent,
        beta_only=beta_only, pending=pending, reviewers=reviewers,
    )


def _hrefs(html):
    return [unescape(href) for href in re.findall(r'<a\b[^>]*\bhref="([^"]+)"', html)]


def _trend(response):
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    points, links = {}, {}
    for year, row in re.findall(
        r'<tr\b[^>]*\bdata-trend-year="(\d+)"[^>]*>(.*?)</tr>',
        response.content.decode(), re.DOTALL,
    ):
        year = int(year)
        assert year not in points
        counts = []
        for marker in ("total", "paper", "patent"):
            (count,) = re.findall(rf"\bdata-trend-{marker}\b[^>]*>\s*(\d+)", row)
            counts.append(int(count))
        assert counts[0] == sum(counts[1:])
        points[year] = tuple(counts)
        (href,) = [href for href in _hrefs(row) if urlsplit(href).path == DETAILS]
        links[year] = href
    assert points, "missing trend rows"
    return points, links


def _counted_ids(response):
    assert response.status_code == 200
    return set(map(int, re.findall(r'data-work-id="(\d+)"\s+data-counted="1"', response.content.decode())))


def test_acceptance_trend_policies_missing_year_and_distinct_school(client, trend_case):
    case = trend_case
    client.force_login(case.school)
    for policy, expected, ids in (
        ("overview", {2024: (2, 1, 1), 2025: (0, 0, 0), 2026: (1, 1, 0)},
         {2024: {case.shared.pk, case.patent.pk}, 2025: set(), 2026: {case.beta_only.pk}}),
        ("strict", {2024: (1, 1, 0), 2025: (0, 0, 0), 2026: (1, 0, 1)},
         {2024: {case.shared.pk}, 2025: set(), 2026: {case.patent.pk}}),
    ):
        points, links = _trend(client.get(TREND, {"year": 2026, "years": 3, "policy": policy}))
        assert points == expected
        for year, href in links.items():
            assert parse_qs(urlsplit(href).query) == {"year": [str(year)], "policy": [policy]}
            assert _counted_ids(client.get(href)) == ids[year]

    # Both departments count this single work; summing departmental points would produce 2.
    shared_query = {"year": 2024, "years": 1, "work_id": case.shared.pk, "kind": "paper"}
    assert _trend(client.get(TREND, shared_query))[0] == {2024: (1, 1, 0)}
    for dept in (case.alpha, case.beta):
        assert _trend(client.get(TREND, {**shared_query, "department": dept.pk}))[0] == {
            2024: (1, 1, 0),
        }
    client.force_login(case.dual)
    assert _trend(client.get(TREND, shared_query))[0] == {2024: (1, 1, 0)}


def test_acceptance_trend_permissions_filters_and_link_scope(client, trend_case):
    case = trend_case
    query = {"year": 2026, "years": 3, "policy": "overview", "kind": "paper"}
    assert client.get(TREND, query).status_code == 302
    client.force_login(case.a.user)
    assert client.get(TREND, query).status_code == 403
    alpha_reviewer = case.reviewers[case.alpha.pk]
    client.force_login(alpha_reviewer)
    assert client.get(TREND, {**query, "department": case.beta.pk}).status_code == 403
    points, links = _trend(client.get(TREND, query))
    assert points == {2024: (1, 1, 0), 2025: (0, 0, 0), 2026: (0, 0, 0)}
    assert _counted_ids(client.get(links[2026])) == set()
    assert case.beta_only.identifier.encode() not in client.get(links[2026]).content
    assert case.b.name.encode() not in client.get(links[2024]).content
    assert _trend(client.get(TREND, {**query, "work_id": case.beta_only.pk}))[0] == dict.fromkeys(
        (2024, 2025, 2026), (0, 0, 0),
    )

    narrowed = {**query, "department": case.alpha.pk, "work_id": case.shared.pk}
    response = client.get(TREND, narrowed)
    points, links = _trend(response)
    assert points == {2024: (1, 1, 0), 2025: (0, 0, 0), 2026: (0, 0, 0)}
    dashboard = client.get("/reports/", {k: v for k, v in narrowed.items() if k != "years"})
    (trend_link,) = [href for href in _hrefs(dashboard.content.decode()) if urlsplit(href).path == TREND]
    assert parse_qs(urlsplit(trend_link).query) == {
        key: [str(value)] for key, value in narrowed.items() if key != "years"
    }
    href = links[2024]
    assert parse_qs(urlsplit(href).query) == {
        key: [str(value)] for key, value in (narrowed | {"year": 2024}).items() if key != "years"
    }
    assert _counted_ids(client.get(href)) == {case.shared.pk}
    client.force_login(case.reviewers[case.beta.pk])
    assert client.get(href).status_code == 403
    client.force_login(alpha_reviewer)
    DepartmentReviewer.objects.filter(user=alpha_reviewer, department=case.alpha).delete()
    assert client.get(TREND, narrowed).status_code == 403


def test_acceptance_trend_range_limits_and_single_year_snapshot(client, trend_case):
    case = trend_case
    client.force_login(case.school)
    for query, expected_years in (
        ({"year": 2026}, range(2022, 2027)),
        ({"year": 2026, "years": 10}, range(2017, 2027)),
        ({"year": 2, "years": 10}, range(1, 3)),
        ({"year": 9999, "years": 1}, range(9999, 10000)),
    ):
        points, _ = _trend(client.get(TREND, query))
        assert list(points) == list(expected_years)
    for invalid in (
        {"years": 0}, {"years": 11}, {"years": "x"}, {"year": 0}, {"year": 10000},
        {"policy": "unknown"}, {"kind": "unknown"}, {"work_id": 0}, {"department": -1},
    ):
        assert client.get(TREND, {"year": 2026, **invalid}).status_code == 400

    query = {"year": 2025, "policy": "overview"}
    before = client.post("/reports/snapshots/freeze/", {**query, "years": 3})
    assert before.status_code == 302
    frozen_url = before["Location"]
    frozen_csv = client.get(f"{frozen_url}export.csv")
    assert frozen_csv.status_code == 200
    Attribution.objects.create(slot=case.pending.slots.get(), researcher=case.a)
    assert _trend(client.get(TREND, {**query, "years": 3}))[0][2025] == (1, 1, 0)
    snap = client.get(frozen_url, {"years": 3})
    assert snap.status_code == 200
    assert b"data-trend-year" not in snap.content
    assert not any(urlsplit(href).path == TREND for href in _hrefs(snap.content.decode()))
    assert b"10.7777/ac-outside" not in frozen_csv.content
    assert b"10.7777/ac-shared" not in frozen_csv.content
    assert client.get(f"{frozen_url}export.csv").content == frozen_csv.content
