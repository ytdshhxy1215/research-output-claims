import csv
import re
from collections import Counter
from dataclasses import replace
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from apps.reports.forms import DepartmentForm, FiltersForm, TrendForm
from apps.reports.services import (
    DEFAULT_TREND_YEARS,
    KINDS,
    POLICIES,
    SOURCE_LABEL,
    ReportFilters,
    freeze_snapshot,
    snapshot_stats,
    snapshots,
    stats,
    trend,
)


def _filters(data):
    return ReportFilters(
        year=data["year"] or timezone.localdate().year,
        policy=data["policy"] or "overview",
        kind=data["kind"],
        work_id=data["work_id"],
    )


def _query(filters, department_id=None):
    return {
        key: value
        for key, value in {
            "year": filters.year,
            "policy": filters.policy,
            "kind": filters.kind,
            "work_id": filters.work_id,
            "department": department_id,
        }.items()
        if value not in (None, "")
    }


def _url(route, filters, department_id=None, snapshot_id=None):
    if snapshot_id is not None:
        name = "snapshot" if route == "index" else f"snapshot_{route}"
        path = reverse(f"reports:{name}", args=[snapshot_id])
        query = {"department": department_id} if department_id is not None else {}
    else:
        path = reverse(f"reports:{route}")
        query = _query(filters, department_id)
    return f"{path}?{urlencode(query)}" if query else path


def _safe_cell(value):
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n")) or re.match(r"^[\s\ufeff]*[=+\-@＝＋－＠]", value)
    ):
        return "'" + value
    return value


def _explanation(fact):
    department = fact["department"]
    labels = [
        f"署名第 {fact['position']} 位：{fact['slot_name']}" if fact["position"] else "无来源署名",
        f"核准人员：{fact['researcher']}" if fact["researcher"] else "",
        f"核准时间：{fact['approved_at']}" if fact["approved_at"] else "",
        f"主院系：{department['name']} ({department['code']})" if department else "",
        "；".join(reason["label"] for reason in fact["reasons"]),
    ]
    return " · ".join(label for label in labels if label)


def _csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="report-{report.filters.policy}-{report.filters.year}.csv"'
    )
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        (
            "work_id",
            "kind",
            "identifier",
            "title",
            "event_date",
            "counted",
            "department_codes",
            "explanations",
        )
    )
    for row in report.rows:
        writer.writerow(
            map(
                _safe_cell,
                (
                    row["work_id"],
                    row["kind"],
                    row["identifier"],
                    row["title"],
                    row["event_date"],
                    int(row["counted"]),
                    ";".join(
                        sorted(
                            {fact["department"]["code"] for fact in row["facts"] if fact["counted"]}
                        )
                    ),
                    "\n".join(_explanation(fact) for fact in row["facts"]),
                ),
            )
        )
    return response


def _show(request, mode, snapshot_id=None):
    scope = DepartmentForm(request.GET)
    form = FiltersForm(request.GET) if snapshot_id is None else None
    if not scope.is_valid() or (form is not None and not form.is_valid()):
        return render(request, "reports/invalid.html", {"scope": scope, "form": form}, status=400)

    department_id = scope.cleaned_data["department"]
    if form is not None:
        report = stats(request.user, _filters(form.cleaned_data), department_id=department_id)
    else:
        report = snapshot_stats(request.user, snapshot_id, department_id=department_id)
    if mode == "csv":
        return _csv(report)

    def url(route, department=department_id):
        return _url(route, report.filters, department, snapshot_id)

    context = {
        "report": report,
        "dashboard_url": url("index"),
        "details_url": url("details"),
        "csv_url": url("csv"),
        "full_scope_url": url("index", None),
        "live_url": _url("index", report.filters, department_id),
    }
    if mode == "details":
        return render(request, "reports/details.html", context)

    context["departments"] = [
        {**department, "details_url": url("details", department["id"])}
        for department in report.departments
    ]
    type_counts = Counter(row["kind"] for row in report.rows if row["counted"])
    context["type_counts"] = [(label, type_counts[kind]) for kind, label in KINDS.items()]
    context["bar_scale"] = max(report.count, 1)
    if snapshot_id is None:
        context["form"] = FiltersForm(initial=_query(report.filters))
        context["trend_url"] = url("trend")
        context["visible_snapshots"] = snapshots(request.user)
        context["can_freeze"] = report.school_count is not None
    return render(request, "reports/index.html", context)


@never_cache
@login_required
@require_safe
def index(request):
    return _show(request, "index")


@never_cache
@login_required
@require_safe
def annual_trend(request):
    scope = DepartmentForm(request.GET)
    form = TrendForm(request.GET)
    if not scope.is_valid() or not form.is_valid():
        return render(request, "reports/invalid.html", {"scope": scope, "form": form}, status=400)

    department_id = scope.cleaned_data["department"]
    filters = _filters(form.cleaned_data)
    years = form.cleaned_data["years"] or DEFAULT_TREND_YEARS
    series = trend(request.user, filters, years=years, department_id=department_id)
    return render(request, "reports/trend.html", {
        "series": series,
        "source_label": SOURCE_LABEL,
        "policy_label": POLICIES[filters.policy],
        "form": TrendForm(initial={**_query(filters), "years": years}),
        "department_id": department_id,
        "annual_url": _url("index", filters, department_id),
        "scale": max(1, *(point.total for point in series.points)),
        "rows": [
            (point, _url("details", replace(filters, year=point.year), department_id))
            for point in series.points
        ],
    })


@never_cache
@login_required
@require_safe
def details(request):
    return _show(request, "details")


@never_cache
@login_required
@require_safe
def export_csv(request):
    return _show(request, "csv")


@transaction.non_atomic_requests
@never_cache
@login_required
@require_POST
def freeze(request):
    form = FiltersForm(request.POST)
    if not form.is_valid():
        return render(request, "reports/invalid.html", {"form": form}, status=400)
    snapshot_id = freeze_snapshot(request.user, _filters(form.cleaned_data))
    return redirect("reports:snapshot", snapshot_id=snapshot_id)


@never_cache
@login_required
@require_safe
def snapshot(request, snapshot_id):
    return _show(request, "index", snapshot_id)


@never_cache
@login_required
@require_safe
def snapshot_details(request, snapshot_id):
    return _show(request, "details", snapshot_id)


@never_cache
@login_required
@require_safe
def snapshot_csv(request, snapshot_id):
    return _show(request, "csv", snapshot_id)
