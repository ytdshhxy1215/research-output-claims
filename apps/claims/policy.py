from enum import StrEnum
from functools import cached_property

from django.db.models import Q

from apps.people.models import DepartmentReviewer, Researcher

from .models import ClaimCase


class Action(StrEnum):
    REVIEW = "review"
    APPEAL = "appeal"
    RESOLVE = "resolve"
    REVOKE = "revoke"
    REASSIGN = "reassign"


FIRST_REVIEW_STATES = (ClaimCase.Status.PENDING, ClaimCase.Status.CONFLICT)
ACTION_STATES = {
    Action.REVIEW: FIRST_REVIEW_STATES,
    Action.APPEAL: (ClaimCase.Status.REJECTED,),
    Action.RESOLVE: (ClaimCase.Status.APPEALED,),
    Action.REVOKE: (ClaimCase.Status.APPROVED,),
    Action.REASSIGN: FIRST_REVIEW_STATES,
}


class ClaimsAccess:
    """Current roles apply to the assigned department, not the applicant's current affiliation."""

    def __init__(self, actor):
        self.actor = actor
        self.active = actor.is_authenticated and actor.is_active

    @cached_property
    def researcher(self):
        if self.active:
            return Researcher.objects.filter(user=self.actor).first()
        return None

    @property
    def researcher_id(self):
        return self.researcher.pk if self.researcher is not None else None

    @cached_property
    def school(self):
        return self.active and self.actor.groups.filter(name="school-reviewers").exists()

    @cached_property
    def department_ids(self):
        if not self.active:
            return frozenset()
        return frozenset(
            DepartmentReviewer.objects.filter(user=self.actor).values_list("department_id", flat=True)
        )

    def own_cases(self):
        return ClaimCase.objects.filter(researcher_id=self.researcher_id)

    def oversight_cases(self):
        cases = ClaimCase.objects.all()
        if not self.school:
            cases = cases.filter(review_department_id__in=self.department_ids)
        if self.researcher_id is not None:
            # A dual-role user must not inspect the other applicants on their own slot.
            cases = cases.exclude(slot_id__in=self.own_cases().values("slot_id"))
        return cases

    def visible_cases(self):
        oversight = self.oversight_cases().order_by().values("pk")
        return ClaimCase.objects.filter(Q(researcher_id=self.researcher_id) | Q(pk__in=oversight))

    def allowed_actions(self, case):
        if self.researcher_id == case.researcher_id:
            return {Action.APPEAL}
        if self.researcher_id is not None and self.own_cases().filter(slot_id=case.slot_id).exists():
            return set()

        actions = set()
        if self.school:
            actions.update((Action.REASSIGN, Action.REVOKE))
            if case.initial_reviewer_id != self.actor.pk:
                actions.add(Action.RESOLVE)
        if case.review_department_id is None:
            if self.school:
                actions.add(Action.REVIEW)
        elif case.review_department_id in self.department_ids:
            actions.add(Action.REVIEW)
        return actions
