# Reports integration

The existing `config` registration mounts `apps.reports.urls` at `/reports/`.

| Django URL name (`reports:`) | Path | Method |
| --- | --- | --- |
| `index` | `/reports/` | GET |
| `trend` | `/reports/trend/` | GET |
| `details` | `/reports/details/` | GET |
| `csv` | `/reports/export.csv` | GET |
| `freeze` | `/reports/snapshots/freeze/` | POST (CSRF; redirects to snapshot) |
| `snapshot` | `/reports/snapshots/<uuid>/` | GET |
| `snapshot_details` | `/reports/snapshots/<uuid>/details/` | GET |
| `snapshot_csv` | `/reports/snapshots/<uuid>/export.csv` | GET |

Live query / freeze form parameters: `year` (defaults to the current local
calendar year), `policy=overview|strict` (defaults to overview), optional
`kind=paper|patent`, optional numeric `work_id`. Reads additionally accept a
numeric `department` ID to select a department in the viewer's current scope.
`trend` interprets `year` as the ending year and accepts `years` (1–10,
default 5). It uses one consistent read, applies the same authorization and work
filters per year, fills missing years with zero, and links to annual details.
A snapshot read uses frozen filters; only `department` can change its projection.
The snapshot is still single-year and has no link to a live trend.

For a management shell or CI smoke, with a school reviewer as `user`:

```python
from apps.reports.services import ReportFilters, freeze_snapshot, snapshot_stats, stats

filters = ReportFilters(year=2025, policy="overview")
live = stats(user, filters)
print(live.school_count, {d["code"]: d["count"] for d in live.departments})
snapshot_id = freeze_snapshot(user, filters)  # outside any enclosing transaction
frozen = snapshot_stats(user, snapshot_id)
```

- School reviewers alone publish. Department reviewers only see their granted
  departments, even on an existing snapshot. Staff/superuser flags alone confer
  no report access. Claims, review routes and evidence are not read by reports.
- `count` is always distinct works in the current projection; `school_count` is
  only set for the school-wide projection. Each detail and CSV row is one work;
  summing CSV `counted` reconciles to `count`. An undated work is an uncounted
  school review item, not a fact attributed to any year or department.
- The snapshot document contains the policy and implementation versions,
  capture time, filters, totals and explanation facts, without foreign keys to
  mutable business rows. PostgreSQL rejects row updates and deletes; database
  owner maintenance (such as truncating or dropping the table) is outside this
  guarantee. Policy keys and the `reports-v1` snapshot read format must remain
  readable when new policy or implementation versions are added.

From the project root with a configured PostgreSQL test database:

```sh
uv run --locked python manage.py migrate
uv run --locked pytest -q tests/test_reports.py
```
