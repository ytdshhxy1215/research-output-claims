from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q

from apps.catalog.models import Work
from apps.matching.recommendations import for_researcher
from apps.sources.models import SourceRecordRevision

from .models import DismissedRecommendation
from .policy import ClaimsAccess


def works_by_identifier(identifier):
    document_alias = SourceRecordRevision.objects.filter(
        record__work_id=OuterRef("pk"), payload__document_number__iexact=identifier,
    )
    return Work.objects.filter(
        Q(identifier__iexact=identifier)
        | (Q(kind=Work.Kind.PATENT) & Q(Exists(document_alias))),
    ).prefetch_related("slots").order_by("pk")


def personal_recommendations(access):
    if access.researcher is None:
        return []
    dismissed = DismissedRecommendation.objects.filter(researcher_id=access.researcher_id)
    hidden = set(dismissed.values_list("slot_id", flat=True))
    hidden.update(access.own_cases().values_list("slot_id", flat=True))
    return [item for item in for_researcher(access.researcher) if item.slot_id not in hidden]


def dismiss_recommendation(*, actor, slot_id):
    access = ClaimsAccess(actor)
    if access.researcher is None:
        raise PermissionDenied
    dismissed = DismissedRecommendation.objects.filter(researcher_id=access.researcher_id)
    if dismissed.filter(slot_id=slot_id).exists():
        return True
    if not any(item.slot_id == slot_id for item in personal_recommendations(access)):
        return False
    dismissed.get_or_create(researcher_id=access.researcher_id, slot_id=slot_id)
    return True
