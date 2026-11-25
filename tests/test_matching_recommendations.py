from datetime import date

import pytest
from django.contrib.auth import get_user_model

from apps.catalog.models import ContributionSlot, Work
from apps.matching.recommendations import for_researcher
from apps.people.models import AffiliationPeriod, Department, Researcher


def _person(username, name):
    return Researcher.objects.create(user=get_user_model().objects.create(username=username), name=name)


def _slot(work, position, name, affiliation):
    return ContributionSlot.objects.create(work=work, position=position, name=name,
                                           affiliation=affiliation, source_slot_id=f"slot-{position}")


@pytest.mark.django_db
def test_adapter_scopes_by_winner_and_supports_demo_department_code_and_label(django_assert_num_queries):
    alpha = Department.objects.create(code="alpha", name="甲院")
    beta = Department.objects.create(code="beta", name="乙院")
    lin = _person("first-lin", "林岚")
    other_lin = _person("second-lin", "林　岚")
    AffiliationPeriod.objects.create(researcher=lin, department=alpha, starts_on=date(2023, 1, 1),
                                     ends_on=date(2026, 1, 1))
    AffiliationPeriod.objects.create(researcher=lin, department=beta, starts_on=date(2026, 1, 1))
    AffiliationPeriod.objects.create(researcher=other_lin, department=beta, starts_on=date(2023, 1, 1))
    paper = Work.objects.create(kind="paper", title="results", identifier="p1",
                                published_on=date(2025, 1, 9))
    expected = _slot(paper, 1, "林·岚", "alpha")
    rival = _slot(paper, 2, "林岚", "乙院")
    _slot(paper, 3, "林岚", "")
    _slot(paper, 4, "陈星", "甲院")
    label = Work.objects.create(kind="paper", title="updated label", identifier="p2",
                                published_on=date(2025, 1, 9))
    expected_label = _slot(label, 1, "林岚", "甲院")
    after_transfer = Work.objects.create(kind="paper", title="overlap", identifier="p3",
                                         published_on=date(2026, 1, 1))
    _slot(after_transfer, 1, "林岚", "乙院")  # Both profiles are in the same department; abstain.

    with django_assert_num_queries(3) as queries:
        recommendations = for_researcher(lin)
    # PostgreSQL streams iterator() with DECLARE ... FOR SELECT.
    statements = [query["sql"].lstrip().upper() for query in queries.captured_queries]
    assert all(sql.startswith("SELECT") or (sql.startswith("DECLARE ") and "FOR SELECT" in sql)
               for sql in statements)
    assert [row.slot_id for row in recommendations] == [expected.pk, expected_label.pk]
    rec = recommendations[0]
    assert rec.work == paper and rec.slot == expected and rec.work_id == paper.pk
    assert (rec.score, rec.margin, rec.algorithm_version) == (70, 42, "weighted-v1")
    assert [item.code for item in rec.evidence] == ["name_exact", "affiliation_match"]
    assert any("alpha" in reason for reason in rec.reasons)
    assert [row.slot_id for row in for_researcher(other_lin)] == [rival.pk]


@pytest.mark.django_db
def test_adapter_handles_patent_dates_primary_only_and_missing_paper_date():
    original = Department.objects.create(code="original", name="原院")
    later = Department.objects.create(code="later", name="新院")
    inventor = _person("inventor", "发明人")
    transfer = date(2025, 1, 1)
    AffiliationPeriod.objects.create(researcher=inventor, department=original,
                                     starts_on=date(2023, 1, 1), ends_on=transfer)
    AffiliationPeriod.objects.create(researcher=inventor, department=later, starts_on=transfer)
    AffiliationPeriod.objects.create(researcher=inventor, department=later,
                                     starts_on=date(2023, 1, 1), is_primary=False)
    patent = Work.objects.create(kind="patent", title="a patent", identifier="pt1",
                                 published_on=date(2024, 1, 1), granted_on=date(2026, 1, 1))
    patent_slot = _slot(patent, 2, "发明人", "原院")
    grant_only = Work.objects.create(kind="patent", title="granted", identifier="pt2",
                                     granted_on=date(2026, 1, 1))
    grant_slot = _slot(grant_only, 1, "发明人", "later")
    paper = Work.objects.create(kind="paper", title="date missing", identifier="p1",
                                granted_on=date(2026, 1, 1))
    _slot(paper, 1, "发明人", "later")
    assert [row.slot_id for row in for_researcher(inventor)] == [patent_slot.pk, grant_slot.pk]


def test_unsaved_researcher_never_queries_or_suggests():
    assert for_researcher(Researcher(pk=123, name="林岚")) == []
