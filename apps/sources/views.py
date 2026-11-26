from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from apps.people.access import SYNC_VIEW_PERMISSION

from .models import SyncRun
from .queue import REPLAY_PERMISSION, REPLAYABLE, SOURCE, ReplayError, requeue


class ReplayForm(forms.Form):
    reason = forms.CharField(label="重放原因", max_length=500, widget=forms.Textarea(attrs={"rows": 3}))
    token = forms.IntegerField(min_value=0, max_value=2**63 - 1, widget=forms.HiddenInput)


def _run(run_id):
    return get_object_or_404(SyncRun.objects.select_related("scope"), pk=run_id, scope__source=SOURCE)


def _detail(request, run, *, form=None, status=200):
    if form is None and run.status in REPLAYABLE and request.user.has_perm(REPLAY_PERMISSION):
        form = ReplayForm(initial={"token": run.fencing_token})
    return render(request, "sync_detail.html", {
        "run": run,
        "form": form,
        "attempts": run.attempt_log.order_by("-fencing_token")[:30],
        "replays": run.replays.select_related("actor")[:30],
    }, status=status)


@never_cache
@permission_required(SYNC_VIEW_PERMISSION, raise_exception=True)
@require_safe
def detail(request, run_id):
    return _detail(request, _run(run_id))


@never_cache
@permission_required((SYNC_VIEW_PERMISSION, REPLAY_PERMISSION), raise_exception=True)
@require_POST
def replay(request, run_id):
    run = _run(run_id)
    form = ReplayForm(request.POST)
    if not form.is_valid():
        return _detail(request, run, form=form, status=400)
    try:
        requeue(run_id, actor=request.user, reason=form.cleaned_data["reason"],
                expected_token=form.cleaned_data["token"])
    except ReplayError as error:
        form.add_error(None, str(error))
        return _detail(request, _run(run_id), form=form, status=409)
    messages.success(request, "重放已排队，等待同步 Worker 处理。")
    return redirect("sync_detail", run_id=run_id)
