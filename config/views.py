from django.contrib.auth.decorators import permission_required
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from apps.people.access import SYNC_VIEW_PERMISSION


@require_safe
def health(request):
    return JsonResponse({"status": "ok"})


@require_safe
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@never_cache
@permission_required(SYNC_VIEW_PERMISSION, raise_exception=True)
@require_safe
def sync_operations(request):
    from apps.sources.models import SourceScope, SyncRun

    return render(request, "sync_operations.html", {
        "scopes": SourceScope.objects.order_by("stream"),
        "runs": SyncRun.objects.select_related("scope").order_by("-created_at", "-pk")[:40],
    })


@never_cache
def permission_denied(request, exception):
    # Report only which work area was requested, never the exception text or target object.
    namespace = getattr(request.resolver_match, "namespace", "")
    return render(request, "403.html", {"reports_denial": namespace == "reports"}, status=403)
