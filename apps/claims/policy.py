from enum import StrEnum

from django.db.models import Q

from apps.people.access import WorkspaceAccess

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


class ClaimsAccess(WorkspaceAccess):
    """Current roles apply to the assigned department, not the applicant's current affiliation."""

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

    def awaiting_decisions(self):
        """Queue of cases where this actor may make an initial or appeal decision now."""
        review_scope = Q(review_department_id__in=self.department_ids)
        decisions = Q()
        if self.school:
            review_scope |= Q(review_department_id__isnull=True)
            decisions = Q(status=ClaimCase.Status.APPEALED) & ~Q(
                initial_reviewer_id=self.actor.pk,
            )
        decisions |= Q(status__in=FIRST_REVIEW_STATES) & review_scope
        # Reassignment is an oversight action, not an initial or appeal decision.
        return self.oversight_cases().filter(decisions).order_by("submitted_at", "pk")

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
