"""Independent black-box acceptance checks for both claims workspaces."""

from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import ContributionSlot, Work
from apps.claims import services
from apps.claims.models import ClaimCase
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher

pytestmark = pytest.mark.django_db
Status = ClaimCase.Status
DOI = "https://doi.org/10.5555/ACCEPTANCE-QUEUE"
REASON = "Synthetic acceptance evidence"
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class ClaimsHTML(HTMLParser):
    """Read only public HTML, not Django context or the queryset under test."""

    def __init__(self, response):
        assert response.status_code == 200
        super().__init__()
        self.body = response.content.decode(response.charset)
        self.stack = []
        self.rows = {}
        self.links = []
        self.forms = []
        self.feed(self.body)
        self.close()

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        section, form = self.stack[-1][1:] if self.stack else (None, None)
        if "data-claim-section" in attrs:
            section = attrs["data-claim-section"]
            assert section not in self.rows, f"duplicate workspace: {section}"
            self.rows[section] = []
        if section and "data-claim-id" in attrs:
            self.rows[section].append(int(attrs["data-claim-id"]))
        if tag == "a" and attrs.get("href"):
            self.links.append((section, attrs["href"]))
        if tag == "form":
            form = (attrs, [])
            self.forms.append(form)
        if tag == "input" and form is not None:
            form[1].append(attrs)
        if tag not in VOID:
            self.stack.append((tag, section, form))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def ids(self, section):
        assert section in self.rows, f"missing data-claim-section={section!r}"
        ids = self.rows[section]
        assert len(ids) == len(set(ids)), f"duplicate claim rows: {ids}"
        return set(ids)

    def navigation(self, section, key, value):
        links = []
        for area, href in self.links:
            target = urlsplit(href)
            if area != section or target.path not in ("", reverse("claims:index")):
                continue
            query = parse_qs(target.query, keep_blank_values=True)
            if query.get(key) == [str(value)]:
                links.append((href, query))
        return links


def index(client, **query):
    return ClaimsHTML(client.get(reverse("claims:index"), query))


def follow(client, href):
    return ClaimsHTML(client.get(urljoin(reverse("claims:index"), href)))


def case_url(action, case):
    return reverse(f"claims:{action}", args=[case.pk])


def assert_actions(client, case, expected):
    detail = ClaimsHTML(client.get(case_url("detail", case)))
    urls = {href for _, href in detail.links}
    actual = {
        action for action in ("review", "resolve", "appeal", "reassign", "revoke")
        if case_url(action, case) in urls
    }
    assert actual == set(expected)
    return detail


def new_user(name, *, teacher=False, school=False, department=None):
    user = get_user_model().objects.create_user(username=f"queue-{name}")
    if teacher:
        researcher = Researcher.objects.create(user=user, name=f"Synthetic {name}")
        if department:
            AffiliationPeriod.objects.create(
                researcher=researcher, department=department,
                starts_on=timezone.localdate() - timedelta(days=1),
            )
    if school:
        group, _ = Group.objects.get_or_create(name="school-reviewers")
        user.groups.add(group)
    return user


def new_slot(key):
    work = Work.objects.create(
        identifier=f"10.5555/{key}", title=f"Synthetic work {key}", kind=Work.Kind.PAPER,
    )
    return ContributionSlot.objects.create(
        work=work, position=1, source_slot_id=key, name="Synthetic contributor",
    )


def submit(user, key, *, slot=None):
    return services.submit_claim(
        actor=user, slot_id=(slot or new_slot(key)).pk, evidence=f"{REASON}: {key}",
    )


def advance(case, *, reviewer, applicant, status, school):
    if status == Status.PENDING:
        return case
    decision = {
        Status.CONFLICT: "conflict", Status.APPROVED: "approve", Status.REVOKED: "approve",
    }.get(status, "reject")
    case = services.review_claim(actor=reviewer, case_id=case.pk, decision=decision, reason=REASON)
    if status in (Status.APPEALED, Status.UPHELD):
        case = services.appeal_claim(actor=applicant, case_id=case.pk, reason=REASON)
    if status == Status.UPHELD:
        case = services.resolve_appeal(
            actor=school, case_id=case.pk, decision="uphold", reason=REASON,
        )
    if status == Status.REVOKED:
        case = services.revoke_attribution(actor=school, case_id=case.pk, reason=REASON)
    assert case.status == status
    return case


@pytest.fixture
def world():
    a = Department.objects.create(code="queue-a", name="Synthetic department A")
    b = Department.objects.create(code="queue-b", name="Synthetic department B")
    reviewer_a = new_user("review-a")
    reviewer_b = new_user("review-b")
    school = new_user("school", school=True)
    peer = new_user("school-peer", school=True)
    dual = new_user("dual", teacher=True, school=True, department=a)
    DepartmentReviewer.objects.create(user=reviewer_a, department=a)
    DepartmentReviewer.objects.create(user=reviewer_b, department=b)
    DepartmentReviewer.objects.create(user=dual, department=a)
    return SimpleNamespace(
        a=a, b=b, reviewer_a=reviewer_a, reviewer_b=reviewer_b, school=school, peer=peer,
        dual=dual, teacher_a=new_user("teacher-a", teacher=True, department=a),
        teacher_b=new_user("teacher-b", teacher=True, department=b),
        unassigned=new_user("unassigned", teacher=True),
    )


@pytest.fixture
def queue(world):
    w = world
    pending_a = submit(w.teacher_a, "pending-a")
    pending_b = submit(w.teacher_b, "pending-b")
    unassigned = submit(w.unassigned, "unassigned")
    conflict_a = advance(
        submit(w.teacher_a, "conflict-a"), reviewer=w.reviewer_a, applicant=w.teacher_a,
        status=Status.CONFLICT, school=w.school,
    )
    appeal_a = advance(
        submit(w.teacher_a, "appeal-a"), reviewer=w.reviewer_a, applicant=w.teacher_a,
        status=Status.APPEALED, school=w.school,
    )
    school_original = advance(
        submit(w.unassigned, "school-original"), reviewer=w.school, applicant=w.unassigned,
        status=Status.APPEALED, school=w.school,
    )
    history_a = advance(
        submit(w.teacher_a, "history-a"), reviewer=w.reviewer_a, applicant=w.teacher_a,
        status=Status.APPROVED, school=w.school,
    )
    dual_own = submit(w.dual, "dual-own")
    competitor = submit(w.teacher_b, "competitor", slot=dual_own.slot)
    competitor = services.reassign_claim(
        actor=w.school, case_id=competitor.pk, department=w.a, reason=REASON,
    )
    return SimpleNamespace(
        pending_a=pending_a, pending_b=pending_b, unassigned=unassigned, conflict_a=conflict_a,
        appeal_a=appeal_a, school_original=school_original, history_a=history_a,
        dual_own=dual_own, competitor=competitor,
    )


def case_ids(*cases):
    return {case.pk for case in cases}


def test_department_default_is_only_actionable_assigned_first_review(client, world, queue):
    q = queue
    client.force_login(world.reviewer_a)
    expected = case_ids(q.pending_a, q.conflict_a, q.dual_own, q.competitor)
    default = index(client)
    assert default.ids("review") == expected
    assert default.navigation("review", "review", "all")
    assert index(client, review="actionable").ids("review") == expected
    assert index(client, review="unexpected").ids("review") == expected
    assert index(client, review="all").ids("review") == expected | case_ids(q.appeal_a, q.history_a)

    assert_actions(client, q.conflict_a, {"review"})
    assert_actions(client, q.appeal_a, set())
    assert_actions(client, q.history_a, set())
    for outside in (q.pending_b, q.unassigned, q.school_original):
        assert client.get(case_url("detail", outside)).status_code == 404


def test_school_queue_distinguishes_decisions_from_reassignment_and_self_review(client, world, queue):
    q = queue
    client.force_login(world.school)
    actionable = case_ids(q.unassigned, q.appeal_a)
    assert index(client).ids("review") == actionable
    assert index(client, review="actionable").ids("review") == actionable
    assert index(client, review="all").ids("review") == case_ids(*vars(q).values())

    assert_actions(client, q.unassigned, {"review", "reassign"})
    assert_actions(client, q.appeal_a, {"resolve"})
    assert_actions(client, q.school_original, set())
    for only_reassign in (q.pending_a, q.pending_b, q.conflict_a):
        assert_actions(client, only_reassign, {"reassign"})
    assert_actions(client, q.history_a, {"revoke"})
    assert client.get(case_url("resolve", q.school_original)).status_code == 403
    client.force_login(world.peer)
    assert index(client).ids("review") == actionable | case_ids(q.school_original)
    assert_actions(client, q.school_original, {"resolve"})


def test_dual_roles_keep_independent_powers_without_exposing_self_or_competitor(client, world, queue):
    q = queue
    client.force_login(world.dual)
    allowed = case_ids(q.pending_a, q.conflict_a, q.unassigned, q.appeal_a, q.school_original)
    default = index(client)
    assert default.ids("mine") == case_ids(q.dual_own)
    assert default.ids("review") == allowed
    history = index(client, review="all")
    assert history.ids("mine") == case_ids(q.dual_own)
    assert history.ids("review") == allowed | case_ids(q.pending_b, q.history_a)
    assert_actions(client, q.pending_a, {"review", "reassign"})
    assert_actions(client, q.school_original, {"resolve"})
    assert_actions(client, q.dual_own, set())
    assert client.get(case_url("review", q.dual_own)).status_code == 403
    assert client.get(case_url("detail", q.competitor)).status_code == 404
    assert client.get(case_url("reassign", q.competitor)).status_code == 404

    client.force_login(world.teacher_b)
    teacher_view = index(client)
    assert teacher_view.ids("mine") == case_ids(q.competitor, q.pending_b)
    assert "review" not in teacher_view.rows
    assert client.get(case_url("detail", q.dual_own)).status_code == 404


def test_grant_revocation_takes_effect_on_next_request_in_same_session(client, world, queue):
    q = queue
    client.force_login(world.dual)
    assert q.pending_a.pk in index(client).ids("review")
    grant = DepartmentReviewer.objects.get(user=world.dual, department=world.a)
    grant.delete()
    assert index(client).ids("review") == case_ids(q.unassigned, q.appeal_a, q.school_original)
    assert q.pending_a.pk in index(client, review="all").ids("review")
    assert_actions(client, q.pending_a, {"reassign"})
    assert client.get(case_url("review", q.pending_a)).status_code == 403

    world.dual.groups.remove(Group.objects.get(name="school-reviewers"))
    for selection in ("actionable", "all"):
        page = index(client, review=selection)
        assert "review" not in page.rows
        assert page.ids("mine") == case_ids(q.dual_own)
    assert client.get(case_url("detail", q.pending_a)).status_code == 404
    assert client.get(case_url("resolve", q.appeal_a)).status_code == 404
    assert_actions(client, q.dual_own, set())


def test_teacher_stage_partitions_and_old_direct_detail(client, world):
    w = world
    cases = {
        status: advance(
            submit(w.teacher_a, f"stage-{status}"), reviewer=w.reviewer_a,
            applicant=w.teacher_a, status=status, school=w.school,
        )
        for status in Status
    }
    other = submit(w.teacher_b, "teacher-not-owned")
    client.force_login(w.teacher_a)
    for stage, expected in (
        (None, set(Status)), ("all", set(Status)), ("unexpected", set(Status)),
        ("in_progress", {Status.PENDING, Status.CONFLICT, Status.APPEALED}),
        ("appeal_available", {Status.REJECTED}),
        ("finished", {Status.APPROVED, Status.UPHELD, Status.REVOKED}),
    ):
        page = index(client, **({"stage": stage} if stage else {}))
        assert page.ids("mine") == case_ids(*(cases[status] for status in expected)), stage
        assert "review" not in page.rows

    for status, case in cases.items():
        detail = assert_actions(client, case, {"appeal"} if status == Status.REJECTED else set())
        assert case.get_status_display() in detail.body
    assert client.get(case_url("detail", other)).status_code == 404


def test_actionable_fifo_and_history_recent_first_break_timestamp_ties_by_pk(client, world):
    w = world
    now = timezone.now()
    with patch("apps.claims.services.timezone.now", return_value=now):
        first_equal = submit(w.teacher_a, "fifo-equal-first")
        second_equal = submit(w.teacher_a, "fifo-equal-second")
    # An earlier timestamp with a later PK distinguishes date ordering from pure PK ordering.
    with patch("apps.claims.services.timezone.now", return_value=now - timedelta(hours=1)):
        oldest = submit(w.teacher_a, "fifo-oldest")
    client.force_login(w.reviewer_a)
    assert index(client).rows["review"] == [oldest.pk, first_equal.pk, second_equal.pk]
    assert index(client, review="all").rows["review"] == [second_equal.pk, first_equal.pk, oldest.pk]


def one_link(page, section, key, value, invariant):
    matches = [
        (href, query) for href, query in page.navigation(section, key, value)
        if all(query.get(k) == [str(v)] for k, v in invariant.items())
    ]
    assert matches, (section, key, value, invariant, page.links)
    assert len({href for href, _ in matches}) == 1, matches
    return matches[0]


def assert_reset(query, field):
    assert query.get(field) in (None, ["1"]), query


def test_dual_workspace_pagination_filter_links_and_doi_search_round_trip(client, world):
    w = world
    lookup_slot = new_slot("acceptance-queue")
    oldest_mine = submit(w.dual, "oldest-mine", slot=lookup_slot)
    first_queue = submit(w.teacher_a, "oldest-queue")
    own = [oldest_mine] + [submit(w.dual, f"owned-page-{i}") for i in range(25)]
    review = [first_queue] + [submit(w.teacher_a, f"review-page-{i}") for i in range(25)]
    ended_mine = advance(
        submit(w.dual, "ended-mine"), reviewer=w.reviewer_a, applicant=w.dual,
        status=Status.APPROVED, school=w.school,
    )
    ended_queue = advance(
        submit(w.teacher_a, "ended-queue"), reviewer=w.reviewer_a, applicant=w.teacher_a,
        status=Status.APPROVED, school=w.school,
    )
    client.force_login(w.dual)
    criteria = {"stage": "in_progress", "review": "actionable", "identifier": DOI}
    first = index(client, **criteria)
    assert first.ids("mine") == case_ids(*own[1:])
    assert first.ids("review") == case_ids(*review[:25])
    assert lookup_slot.work.title in first.body
    assert (None, case_url("detail", oldest_mine)) in first.links

    mine_next, _ = one_link(first, "mine", "mine", 2, criteria)
    mine_second = follow(client, mine_next)
    assert mine_second.ids("mine") == case_ids(oldest_mine)
    assert mine_second.ids("review") == first.ids("review")
    queue_next, _ = one_link(mine_second, "review", "queue", 2, criteria | {"mine": 2})
    second = follow(client, queue_next)
    assert second.ids("mine") == case_ids(oldest_mine)
    assert second.ids("review") == case_ids(review[-1])
    assert lookup_slot.work.title in second.body

    mine_previous, _ = one_link(second, "mine", "mine", 1, criteria | {"queue": 2})
    back_mine = follow(client, mine_previous)
    assert back_mine.ids("mine") == first.ids("mine")
    assert back_mine.ids("review") == second.ids("review")
    queue_previous, _ = one_link(second, "review", "queue", 1, criteria | {"mine": 2})
    back_queue = follow(client, queue_previous)
    assert back_queue.ids("review") == first.ids("review")
    assert back_queue.ids("mine") == second.ids("mine")

    switch_mine, mine_query = one_link(second, "mine", "stage", "finished", {
        "review": "actionable", "identifier": DOI, "queue": 2,
    })
    assert_reset(mine_query, "mine")
    mine_finished = follow(client, switch_mine)
    assert mine_finished.ids("mine") == case_ids(ended_mine)
    assert mine_finished.ids("review") == case_ids(review[-1])
    switch_review, review_query = one_link(second, "review", "review", "all", {
        "stage": "in_progress", "identifier": DOI, "mine": 2,
    })
    assert_reset(review_query, "queue")
    history = follow(client, switch_review)
    assert history.ids("mine") == case_ids(oldest_mine)
    assert history.ids("review") == case_ids(ended_queue, *review[2:])

    search_forms = [
        (attrs, fields) for attrs, fields in second.forms
        if any(field.get("name") == "identifier" for field in fields)
    ]
    assert len(search_forms) == 1
    attrs, fields = search_forms[0]
    assert attrs.get("method", "get").lower() == "get"
    hidden = {
        field["name"]: field.get("value", "") for field in fields
        if field.get("type", "").lower() == "hidden" and field.get("name")
    }
    assert {k: hidden.get(k) for k in ("stage", "review", "mine", "queue")} == {
        "stage": "in_progress", "review": "actionable", "mine": "2", "queue": "2",
    }
    search = ClaimsHTML(client.get(
        attrs.get("action") or reverse("claims:index"),
        hidden | {"identifier": "10.5555/ended-mine"},
    ))
    assert (None, case_url("detail", ended_mine)) in search.links
    assert search.ids("mine") == second.ids("mine")
    assert search.ids("review") == second.ids("review")
