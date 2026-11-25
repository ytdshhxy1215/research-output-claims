from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Case, F, Q, When
from django.db.models.functions import Least
from django.http import Http404
from django.utils import timezone

from apps.catalog.models import Work
from apps.claims.models import Attribution
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer
from apps.reports.models import ReportSnapshot

POLICIES = {"overview": "成果概览 v1", "strict": "学院考核示例 v1"}
POLICY_RULES = {
    "overview": "任一署名已核准即可计入。论文取首次正式发表日，专利取可证的公开日、授权日中较早的一日。",
    "strict": "仅计入来源首位署名已核准的成果。论文取首次正式发表日，专利必须有授权日并按授权日统计。",
}
KINDS = {"paper": "论文", "patent": "专利"}
IMPLEMENTATION_VERSION = "reports-v1"
SOURCE_LABEL = "模拟源 / 合成数据"
REASONS = {
    "counted": "已核准，按事件日主院系计入",
    "not_approved": "无已核准归属",
    "revoked": "归属已撤销",
    "missing_slot": "来源缺少署名",
    "not_first": "不是来源首位署名",
    "unknown_first": "来源没有唯一首位署名，待核对",
    "missing_publication": "缺少正式发表日，待核对",
    "missing_patent_date": "缺少公开日和授权日，待核对",
    "missing_grant": "缺少授权日，待核对",
    "missing_primary": "事件日无主归属院系，待核对",
    "ambiguous_primary": "事件日主归属重叠，待核对",
}


@dataclass(frozen=True)
class ReportFilters:
    year: int
    policy: str = "overview"
    kind: str = ""
    work_id: int | None = None

    def __post_init__(self):
        if (
            not 1 <= self.year <= 9999
            or self.policy not in POLICIES
            or (self.kind and self.kind not in KINDS)
            or (self.work_id is not None and not 1 <= self.work_id < 2**63)
        ):
            raise ValueError("Invalid report filters")


@dataclass(frozen=True)
class Report:
    filters: ReportFilters
    policy_label: str
    policy_rule: str
    implementation_version: str
    source_label: str
    captured_at: datetime
    scope_label: str
    department_id: int | None
    departments: tuple[dict, ...]
    school_count: int | None
    count: int
    rows: tuple[dict, ...]
    snapshot_id: UUID | None = None


@dataclass(frozen=True)
class _Viewer:
    school: bool
    department_ids: frozenset[int]

    def require_department(self, department_id):
        if (
            department_id is not None
            and not self.school
            and department_id not in self.department_ids
        ):
            raise PermissionDenied


def _viewer(user):
    if not user.is_authenticated:
        raise PermissionDenied
    active = get_user_model().objects.filter(pk=user.pk, is_active=True)
    if active.filter(groups__name="school-reviewers").exists():
        return _Viewer(True, frozenset())
    ids = frozenset(
        DepartmentReviewer.objects.filter(user__in=active).values_list("department_id", flat=True)
    )
    if not ids:
        raise PermissionDenied
    return _Viewer(False, ids)


@contextmanager
def _point_in_time(*, publish=False):
    if connection.in_atomic_block or not connection.get_autocommit():
        if publish:
            raise RuntimeError("Freeze a report outside any enclosing transaction")
        # A caller-owned transaction already controls read isolation (e.g. Django TestCase).
        yield timezone.now()
        return
    with transaction.atomic(durable=publish):
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            # The first data statement pins the MVCC snapshot and supplies its as-of time.
            cursor.execute("SELECT statement_timestamp(), pg_current_snapshot()")
            captured_at = cursor.fetchone()[0]
        yield captured_at


def _work_query(filters):
    patent_date = (
        F("granted_on") if filters.policy == "strict" else Least("published_on", "granted_on")
    )
    work = Work.objects.annotate(
        event_date=Case(When(kind="patent", then=patent_date), default=F("published_on"))
    ).filter(Q(event_date__year=filters.year) | Q(event_date__isnull=True))
    if filters.kind:
        work = work.filter(kind=filters.kind)
    if filters.work_id is not None:
        work = work.filter(pk=filters.work_id)
    return work.prefetch_related("slots").order_by("event_date", "pk")


def _affiliations(attributions, event_dates):
    periods = defaultdict(list)
    if attributions and event_dates:
        for period in AffiliationPeriod.objects.filter(
            researcher_id__in={a.researcher_id for a in attributions.values()},
            is_primary=True,
            starts_on__lte=max(event_dates),
        ).filter(Q(ends_on__isnull=True) | Q(ends_on__gt=min(event_dates))):
            periods[period.researcher_id].append(period)
    return periods


def _fact(work, slot, attribution, periods, policy, first_count, captured_at):
    reasons = []
    department_id = None
    if slot is None:
        reasons.append("missing_slot")
    elif attribution is None:
        reasons.append("not_approved")
    elif attribution.valid_until is not None and attribution.valid_until <= captured_at:
        reasons.append("revoked")

    if slot is not None and policy == "strict":
        if first_count != 1:
            reasons.append("unknown_first")
        elif slot.position != 1:
            reasons.append("not_first")

    if work.event_date is None:
        if work.kind == "paper":
            reasons.append("missing_publication")
        else:
            reasons.append("missing_grant" if policy == "strict" else "missing_patent_date")
    elif attribution is not None:
        matches = [
            p
            for p in periods[attribution.researcher_id]
            if p.starts_on <= work.event_date and (p.ends_on is None or work.event_date < p.ends_on)
        ]
        if len(matches) == 1:
            department_id = str(matches[0].department_id)
        else:
            reasons.append("ambiguous_primary" if matches else "missing_primary")

    return {
        "department_id": department_id,
        "position": slot.position if slot else None,
        "slot_name": slot.name if slot else "",
        "researcher": attribution.researcher.name if attribution else "",
        "approved_at": attribution.valid_from.isoformat() if attribution else None,
        "counted": not reasons,
        "reasons": [{"code": code, "label": REASONS[code]} for code in reasons or ["counted"]],
    }


def _build_payload(filters, captured_at):
    departments = {
        str(pk): {"id": pk, "code": code, "name": name, "count": 0}
        for pk, code, name in Department.objects.order_by("code").values_list("pk", "code", "name")
    }
    works = list(_work_query(filters))
    slot_ids = [slot.pk for work in works for slot in work.slots.all()]
    attributions = {}
    for item in (
        Attribution.objects.filter(
            slot_id__in=slot_ids,
            valid_from__lte=captured_at,
        )
        .select_related("researcher")
        .order_by("-valid_from", "-pk")
    ):
        # Prefer a currently effective attribution; otherwise explain the latest revocation.
        previous = attributions.get(item.slot_id)
        if previous is None or (
            previous.valid_until is not None
            and previous.valid_until <= captured_at
            and (item.valid_until is None or item.valid_until > captured_at)
        ):
            attributions[item.slot_id] = item
    periods = _affiliations(attributions, [work.event_date for work in works if work.event_date])

    rows = []
    school_count = 0
    for work in works:
        slots = list(work.slots.all())
        first_count = sum(slot.position == 1 for slot in slots)
        facts = [
            _fact(
                work,
                slot,
                attributions.get(slot.pk) if slot else None,
                periods,
                filters.policy,
                first_count,
                captured_at,
            )
            for slot in slots or [None]
        ]
        counted_departments = {fact["department_id"] for fact in facts if fact["counted"]}
        school_count += bool(counted_departments)
        for key in counted_departments:
            departments[key]["count"] += 1
        rows.append(
            {
                "work_id": work.pk,
                "kind": work.kind,
                "kind_label": KINDS.get(work.kind, work.kind),
                "identifier": work.identifier,
                "title": work.title,
                "event_date": work.event_date.isoformat() if work.event_date else None,
                "facts": facts,
            }
        )
    return {
        "source_label": SOURCE_LABEL,
        "policy_rule": POLICY_RULES[filters.policy],
        "school_count": school_count,
        "target_department_ids": sorted(
            {
                fact["department_id"]
                for row in rows
                for fact in row["facts"]
                if fact["department_id"] is not None
            }
        )
        if filters.work_id is not None
        else [],
        "departments": departments,
        "rows": rows,
    }


def _project(payload, viewer, filters, captured_at, department_id=None, snapshot=None):
    viewer.require_department(department_id)
    all_departments = payload["departments"]
    school = viewer.school and department_id is None
    if school:
        departments = all_departments
        scope_label = "校级"
    elif department_id is not None:
        key = str(department_id)
        if key not in all_departments:
            raise Http404
        departments = {key: all_departments[key]}
        scope_label = departments[key]["name"]
    else:
        departments = {
            key: department
            for key, department in all_departments.items()
            if department["id"] in viewer.department_ids
        }
        if not departments and snapshot is not None:
            raise Http404
        scope_label = "授权院系范围"

    rows = []
    for row in payload["rows"]:
        facts = [
            {**fact, "department": departments.get(fact["department_id"])}
            for fact in row["facts"]
            if school or fact["department_id"] in departments
        ]
        if facts:
            rows.append({**row, "facts": facts, "counted": any(fact["counted"] for fact in facts)})
    return Report(
        filters=filters,
        policy_label=snapshot.policy_label if snapshot else POLICIES[filters.policy],
        policy_rule=payload["policy_rule"],
        implementation_version=snapshot.implementation_version
        if snapshot
        else IMPLEMENTATION_VERSION,
        source_label=payload["source_label"],
        captured_at=captured_at,
        snapshot_id=snapshot.pk if snapshot else None,
        scope_label=scope_label,
        department_id=department_id,
        departments=tuple(sorted(departments.values(), key=lambda department: department["code"])),
        school_count=payload["school_count"] if school else None,
        count=payload["school_count"] if school else sum(row["counted"] for row in rows),
        rows=tuple(rows),
    )


def stats(user, filters: ReportFilters, *, department_id=None) -> Report:
    """Return live, distinct totals and explanation rows projected to current grants."""
    with _point_in_time() as captured_at:
        viewer = _viewer(user)
        viewer.require_department(department_id)
        return _project(
            _build_payload(filters, captured_at), viewer, filters, captured_at, department_id
        )


def freeze_snapshot(user, filters: ReportFilters) -> UUID:
    """Publish a whole-school snapshot. Own the transaction, including the final insert."""
    with _point_in_time(publish=True) as captured_at:
        if not _viewer(user).school:
            raise PermissionDenied
        return ReportSnapshot.objects.create(
            captured_at=captured_at,
            created_by_id=user.pk,
            policy=filters.policy,
            policy_label=POLICIES[filters.policy],
            implementation_version=IMPLEMENTATION_VERSION,
            filters=asdict(filters),
            payload=_build_payload(filters, captured_at),
        ).pk


def _visible_snapshots(viewer, department_id=None):
    snapshots = ReportSnapshot.objects.all()
    if not viewer.school:
        ids = [str(department_id)] if department_id else [str(pk) for pk in viewer.department_ids]
        # A single-work filter is itself identifying metadata. Hide it if its facts
        # have no authorized contribution, even when the department existed at capture.
        snapshots = snapshots.filter(payload__departments__has_any_keys=ids).filter(
            Q(filters__work_id=None) | Q(payload__target_department_ids__has_any_keys=ids)
        )
    return snapshots


def snapshots(user):
    """List current-scope metadata; never return a snapshot's unprojected document."""
    with _point_in_time():
        return list(
            _visible_snapshots(_viewer(user)).values(
                "id",
                "captured_at",
                "policy_label",
                "filters",
            )[:25]
        )


def snapshot_stats(user, snapshot_id, *, department_id=None) -> Report:
    with _point_in_time():
        viewer = _viewer(user)
        viewer.require_department(department_id)
        try:
            snapshot = _visible_snapshots(viewer, department_id).get(pk=snapshot_id)
        except ReportSnapshot.DoesNotExist as exc:
            raise Http404 from exc
        return _project(
            snapshot.payload,
            viewer,
            ReportFilters(**snapshot.filters),
            snapshot.captured_at,
            department_id,
            snapshot,
        )
