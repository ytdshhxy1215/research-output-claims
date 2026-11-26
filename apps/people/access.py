"""Per-request workspace roles shared by presentation and domain policies."""

from functools import cached_property

from django.contrib.auth import get_user_model

from apps.people.models import DepartmentReviewer, Researcher

SCHOOL_REVIEWER_GROUP = "school-reviewers"
SYNC_VIEW_PERMISSION = "sources.view_syncrun"


class WorkspaceAccess:
    def __init__(self, actor):
        self.actor = actor
        self.active = actor.is_authenticated and actor.is_active

    @cached_property
    def researcher(self):
        if self.active:
            return Researcher.objects.filter(user_id=self.actor.pk, user__is_active=True).first()
        return None

    @property
    def researcher_id(self):
        return self.researcher.pk if self.researcher is not None else None

    @cached_property
    def school(self):
        return self.active and get_user_model().objects.filter(
            pk=self.actor.pk, is_active=True, groups__name=SCHOOL_REVIEWER_GROUP,
        ).exists()

    @cached_property
    def department_ids(self):
        if not self.active:
            return frozenset()
        return frozenset(
            DepartmentReviewer.objects.filter(user_id=self.actor.pk, user__is_active=True)
            .values_list("department_id", flat=True)
        )

    @property
    def can_view_claims(self):
        return self.can_view_reports or self.researcher_id is not None

    @property
    def can_view_reports(self):
        return self.school or bool(self.department_ids)

    @cached_property
    def can_view_sync(self):
        return self.active and self.actor.has_perm(SYNC_VIEW_PERMISSION)

    @property
    def role_labels(self):
        return tuple(label for enabled, label in (
            (self.researcher_id is not None, "教师"),
            (bool(self.department_ids), "院系审核员"),
            (self.school, "校级复核员"),
            (self.can_view_sync, "同步运维"),
        ) if enabled)
