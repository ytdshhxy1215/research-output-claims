from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import ContributionSlot
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer

from .models import Attribution, ClaimCase, ClaimEvent
from .policy import ACTION_STATES, Action, ClaimsAccess

Status = ClaimCase.Status
Kind = ClaimEvent.Kind


class WorkflowConflict(Exception):
    """The requested transition is not possible without changing the current state."""


class InvalidInput(ValueError):
    pass


def _text(value, label, limit, *, required=True):
    if not isinstance(value, str):
        raise InvalidInput(f"{label}无效。")
    value = value.strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise InvalidInput(f"{label}必须填写有效文字，且不得超过 {limit} 字。")
    return value


def _eligible_department_reviewers(slot_id, department_id, *, applicant_id=None):
    party_ids = ClaimCase.objects.filter(slot_id=slot_id).values("researcher__user_id")
    reviewers = DepartmentReviewer.objects.filter(department_id=department_id, user__is_active=True)
    if applicant_id is not None:
        reviewers = reviewers.exclude(user_id=applicant_id)
    return reviewers.exclude(user_id__in=party_ids)


def _route_at_submission(slot_id, researcher_id, actor_id, day):
    periods = AffiliationPeriod.objects.filter(
        researcher_id=researcher_id, is_primary=True, starts_on__lte=day,
    )
    primary_ids = list(
        periods.filter(Q(ends_on__isnull=True) | Q(ends_on__gt=day))
        .order_by("pk").values_list("department_id", flat=True)[:2]
    )
    if len(primary_ids) != 1:
        return None, "无唯一有效主责院系，进入校级待分派。"
    department_id = primary_ids[0]
    if not _eligible_department_reviewers(slot_id, department_id, applicant_id=actor_id).exists():
        return None, "主责院系无合格审核人，进入校级待分派。"
    return department_id, "按提交时的主责院系分派。"


def _record(case, actor, kind, reason, *, previous_status="", previous_department=None, now):
    ClaimEvent.objects.create(
        case=case, actor=actor, kind=kind, reason=reason, occurred_at=now,
        from_status=previous_status, to_status=case.status,
        from_department_id=previous_department, to_department_id=case.review_department_id,
    )


@transaction.atomic
def submit_claim(*, actor, slot_id, evidence, evidence_identifier=""):
    access = ClaimsAccess(actor)
    if access.researcher_id is None:
        raise PermissionDenied
    evidence = _text(evidence, "证据说明", 4000)
    identifier = _text(evidence_identifier, "证据标识符", 255, required=False)
    slot = ContributionSlot.objects.select_for_update().get(pk=slot_id)
    if access.own_cases().filter(slot=slot).exists():
        raise WorkflowConflict("你已申请过此署名。初审驳回只能从原申请提出一次申诉。")
    now = timezone.now()
    department_id, route_reason = _route_at_submission(
        slot.pk, access.researcher_id, actor.pk, timezone.localdate(now),
    )
    case = ClaimCase.objects.create(
        slot=slot, researcher_id=access.researcher_id, review_department_id=department_id,
        evidence=evidence, evidence_identifier=identifier, submitted_at=now,
    )
    _record(case, actor, Kind.SUBMITTED, route_reason, now=now)
    return case


def _locked_case(actor, case_id, action):
    # All writers (including new cases) lock slot first, then case. A slot lock
    # serializes approvals and revocations even when no Attribution row exists.
    slot_id = ClaimCase.objects.values_list("slot_id", flat=True).get(pk=case_id)
    ContributionSlot.objects.select_for_update().only("pk").get(pk=slot_id)
    case = ClaimCase.objects.select_for_update().get(pk=case_id, slot_id=slot_id)
    if action not in ClaimsAccess(actor).allowed_actions(case):
        raise PermissionDenied
    if case.status not in ACTION_STATES[action]:
        raise WorkflowConflict("当前申请状态不允许此操作。请刷新后重试。")
    return case


def _transition(case, actor, kind, status, reason, *, now, initial_review=False):
    previous = case.status
    case.status = status
    fields = ["status"]
    if initial_review:
        case.initial_reviewer = actor
        fields.append("initial_reviewer")
    case.save(update_fields=fields)
    _record(
        case, actor, kind, reason, previous_status=previous,
        previous_department=case.review_department_id, now=now,
    )
    return case


def _approve(case, now):
    if Attribution.objects.filter(slot_id=case.slot_id, valid_until__isnull=True).exists():
        raise WorkflowConflict("此署名已有有效归属。校级须先显式撤销旧归属，才能核准其他申请。")
    Attribution.objects.create(
        slot_id=case.slot_id, researcher_id=case.researcher_id, valid_from=now,
    )


@transaction.atomic
def review_claim(*, actor, case_id, decision, reason):
    case = _locked_case(actor, case_id, Action.REVIEW)
    reason = _text(reason, "初审理由", 4000)
    now = timezone.now()
    if decision == "approve":
        _approve(case, now)
        kind, status = Kind.APPROVED, Status.APPROVED
    elif decision == "reject":
        kind, status = Kind.REJECTED, Status.REJECTED
    elif decision == "conflict":
        if case.status == Status.CONFLICT:
            raise WorkflowConflict("此申请已经标记为署名冲突。")
        kind, status = Kind.CONFLICT, Status.CONFLICT
    else:
        raise InvalidInput("请选择有效的初审决定。")
    return _transition(case, actor, kind, status, reason, now=now, initial_review=decision != "conflict")


@transaction.atomic
def appeal_claim(*, actor, case_id, reason):
    case = _locked_case(actor, case_id, Action.APPEAL)
    reason = _text(reason, "申诉补充说明", 4000)
    return _transition(case, actor, Kind.APPEALED, Status.APPEALED, reason, now=timezone.now())


@transaction.atomic
def resolve_appeal(*, actor, case_id, decision, reason):
    case = _locked_case(actor, case_id, Action.RESOLVE)
    reason = _text(reason, "复核理由", 4000)
    now = timezone.now()
    if decision == "approve":
        _approve(case, now)
        kind, status = Kind.OVERTURNED, Status.APPROVED
    elif decision == "uphold":
        kind, status = Kind.UPHELD, Status.UPHELD
    else:
        raise InvalidInput("请选择有效的复核决定。")
    return _transition(case, actor, kind, status, reason, now=now)


@transaction.atomic
def revoke_attribution(*, actor, case_id, reason):
    case = _locked_case(actor, case_id, Action.REVOKE)
    reason = _text(reason, "撤销理由", 4000)
    attribution = Attribution.objects.select_for_update().filter(
        slot_id=case.slot_id, researcher_id=case.researcher_id, valid_until__isnull=True,
    ).first()
    if attribution is None:
        raise WorkflowConflict("此申请没有可撤销的有效归属。")
    now = timezone.now()
    attribution.valid_until = now
    attribution.save(update_fields=["valid_until"])
    return _transition(case, actor, Kind.REVOKED, Status.REVOKED, reason, now=now)


@transaction.atomic
def reassign_claim(*, actor, case_id, department: Department | None, reason):
    case = _locked_case(actor, case_id, Action.REASSIGN)
    reason = _text(reason, "转派理由", 4000)
    department_id = department.pk if department is not None else None
    previous = case.review_department_id
    if department_id == previous:
        raise WorkflowConflict("请选择不同的审核管辖。")
    if department_id is not None and not _eligible_department_reviewers(
        case.slot_id, department_id,
    ).exists():
        raise WorkflowConflict("所选院系没有合格审核人。")
    case.review_department_id = department_id
    case.save(update_fields=["review_department"])
    _record(
        case, actor, Kind.REASSIGNED, reason, previous_status=case.status,
        previous_department=previous, now=timezone.now(),
    )
    return case
