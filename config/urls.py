from django.contrib import admin
from django.urls import include, path
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from apps.sources import views as source_views
from config import views

urlpatterns = [
    path("", never_cache(TemplateView.as_view(template_name="home.html")), name="home"),
    path("healthz", views.health, name="health"),
    path("readyz", views.ready, name="ready"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    path("claims/", include("apps.claims.urls")),
    path("operations/sync/", views.sync_operations, name="sync_operations"),
    path("operations/sync/<int:run_id>/", source_views.detail, name="sync_detail"),
    path("operations/sync/<int:run_id>/replay/", source_views.replay, name="sync_replay"),
    path("reports/", include("apps.reports.urls")),
]

handler403 = views.permission_denied
