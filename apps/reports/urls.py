from django.urls import path

from apps.reports import views

app_name = "reports"
urlpatterns = [
    path("", views.index, name="index"),
    path("details/", views.details, name="details"),
    path("trend/", views.annual_trend, name="trend"),
    path("export.csv", views.export_csv, name="csv"),
    path("snapshots/freeze/", views.freeze, name="freeze"),
    path("snapshots/<uuid:snapshot_id>/", views.snapshot, name="snapshot"),
    path("snapshots/<uuid:snapshot_id>/details/", views.snapshot_details, name="snapshot_details"),
    path("snapshots/<uuid:snapshot_id>/export.csv", views.snapshot_csv, name="snapshot_csv"),
]
