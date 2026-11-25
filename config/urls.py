from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from config import views

urlpatterns = [
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
    path("healthz", views.health, name="health"),
    path("readyz", views.ready, name="ready"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    path("claims/", include("apps.claims.urls")),
    path("operations/sync/", views.sync_operations, name="sync_operations"),
    path("reports/", include("apps.reports.urls")),
]
