from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.catalog.models import ContributionSlot

from . import discovery, forms, services
from .models import ClaimCase, ClaimEvent
from .policy import ACTION_STATES, Action, ClaimsAccess

ACTION_FORMS = {
    Action.REVIEW: (forms.ReviewForm, services.review_claim, "记录初审决定"),
    Action.APPEAL: (forms.AppealForm, services.appeal_claim, "提交申诉"),
    Action.RESOLVE: (forms.ResolveForm, services.resolve_appeal, "记录复核决定"),
    Action.REVOKE: (forms.ReasonForm, services.revoke_attribution, "撤销归属"),
    Action.REASSIGN: (forms.ReassignForm, services.reassign_claim, "转派审核"),
}
INTERNAL_EVENTS = (ClaimEvent.Kind.REASSIGNED, ClaimEvent.Kind.CONFLICT)
TEACHER_STAGES = {
    "all": ("全部申请", ()),
    "in_progress": ("进行中", (
        ClaimCase.Status.PENDING, ClaimCase.Status.CONFLICT, ClaimCase.Status.APPEALED,
    )),
    "appeal_available": ("可申诉", (ClaimCase.Status.REJECTED,)),
    "finished": ("已结束", (
        ClaimCase.Status.APPROVED, ClaimCase.Status.UPHELD, ClaimCase.Status.REVOKED,
    )),
}
REVIEW_FILTERS = {"actionable": "待我处理", "all": "全部授权记录"}


def _case_rows(queryset):
    return queryset.select_related("slot__work", "researcher", "review_department")


def _visible_case(access, case_id):
    return get_object_or_404(_case_rows(access.visible_cases()), pk=case_id)


@never_cache
@login_required
@require_safe
def index(request):
    access = ClaimsAccess(request.user)
    form = forms.IdentifierSearchForm(request.GET if "identifier" in request.GET else None)
    found = None
    if form.is_bound:
        if access.researcher_id is None:
            raise PermissionDenied
        if form.is_valid():
            identifier = form.cleaned_data["identifier"]
            found = list(discovery.works_by_identifier(identifier))
            own_by_slot = dict(access.own_cases().filter(
                slot__work_id__in=[work.pk for work in found],
            ).values_list("slot_id", "pk"))
            for work in found:
                for slot in work.slots.all():
                    slot.own_case_id = own_by_slot.get(slot.pk)

    is_researcher = access.researcher_id is not None
    is_reviewer = access.can_view_reports
    stage = request.GET.get("stage", "all")
    stage = stage if stage in TEACHER_STAGES else "all"
    review = request.GET.get("review", "actionable")
    review = review if review in REVIEW_FILTERS else "actionable"

    my_page = review_page = None
    if is_researcher:
        own_cases = access.own_cases()
        if statuses := TEACHER_STAGES[stage][1]:
            own_cases = own_cases.filter(status__in=statuses)
        my_page = Paginator(_case_rows(own_cases), 25).get_page(request.GET.get("mine"))
    if is_reviewer:
        review_cases = (
            access.awaiting_decisions() if review == "actionable" else access.oversight_cases()
        )
        review_page = Paginator(_case_rows(review_cases), 25).get_page(request.GET.get("queue"))

    search_state = {key: request.GET[key] for key in ("mine", "queue") if key in request.GET}
    if "stage" in request.GET:
        search_state["stage"] = stage
    if "review" in request.GET:
        search_state["review"] = review
    return render(request, "claims/index.html", {
        "form": form,
        "found": found,
        "recommendations": discovery.personal_recommendations(access),
        "is_researcher": is_researcher,
        "is_reviewer": is_reviewer,
        "my_page": my_page,
        "review_page": review_page,
        "stage": stage,
        "teacher_stages": [(key, item[0]) for key, item in TEACHER_STAGES.items()],
        "review": review,
        "review_filters": REVIEW_FILTERS.items(),
        "search_state": search_state.items(),
    })


@never_cache
@login_required
@require_safe
def detail(request, case_id):
    access = ClaimsAccess(request.user)
    case = _visible_case(access, case_id)
    can_act = access.allowed_actions(case)
    is_owner = case.researcher_id == access.researcher_id
    events = case.events.all()
    if is_owner:
        events = events.exclude(kind__in=INTERNAL_EVENTS)
    else:
        events = events.select_related("actor", "from_department", "to_department")
    return render(request, "claims/detail.html", {
        "case": case,
        "status_label": case.get_status_display(),
        "is_owner": is_owner,
        "events": events,
        "actions": [
            action.value for action in Action
            if action in can_act and case.status in ACTION_STATES[action]
        ],
    })


@never_cache
@login_required
@sensitive_post_parameters()
@require_http_methods(("GET", "HEAD", "POST"))
def submit(request, slot_id):
    access = ClaimsAccess(request.user)
    if access.researcher_id is None:
        raise PermissionDenied
    slot = get_object_or_404(ContributionSlot.objects.select_related("work"), pk=slot_id)
    existing = access.own_cases().filter(slot=slot).first()
    if existing is not None and request.method != "POST":
        return redirect("claims:detail", case_id=existing.pk)
    form = forms.SubmitClaimForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                case = services.submit_claim(actor=request.user, slot_id=slot_id, **form.cleaned_data)
            except ContributionSlot.DoesNotExist as exc:
                raise Http404 from exc
            except services.WorkflowConflict as exc:
                form.add_error(None, str(exc))
                status = 409
            except services.InvalidInput as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "认领申请已提交。")
                return redirect("claims:detail", case_id=case.pk)
    return render(request, "claims/form.html", {
        "form": form, "title": "提交认领申请", "slot": slot,
    }, status=status)


@never_cache
@login_required
@sensitive_post_parameters()
@require_http_methods(("GET", "HEAD", "POST"))
def case_action(request, case_id, action):
    access = ClaimsAccess(request.user)
    case = _visible_case(access, case_id)
    if action not in access.allowed_actions(case):
        raise PermissionDenied
    form_class, handler, title = ACTION_FORMS[action]
    form = form_class(request.POST if request.method == "POST" else None)
    status = 200
    if case.status not in ACTION_STATES[action]:
        return render(request, "claims/form.html", {
            "title": title, "case": case, "state_error": "当前申请状态不允许此操作。请刷新后重试。",
        }, status=409)
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                handler(actor=request.user, case_id=case_id, **form.cleaned_data)
            except ClaimCase.DoesNotExist as exc:
                raise Http404 from exc
            except services.WorkflowConflict as exc:
                form.add_error(None, str(exc))
                status = 409
            except services.InvalidInput as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"操作已记录：{title}。")
                return redirect("claims:detail", case_id=case.pk)
    return render(request, "claims/form.html", {
        "form": form, "title": title, "case": case, "action": action.value,
    }, status=status)


@never_cache
@login_required
@require_POST
def dismiss(request, slot_id):
    if not discovery.dismiss_recommendation(actor=request.user, slot_id=slot_id):
        raise Http404
    messages.success(request, "已收起该建议；你仍可按成果标识主动申请。")
    return redirect("claims:index")
