from django.urls import path

from . import views
from .policy import Action

app_name = "claims"

urlpatterns = [
    path("", views.index, name="index"),
    path("slots/<int:slot_id>/submit/", views.submit, name="submit"),
    path("slots/<int:slot_id>/dismiss/", views.dismiss, name="dismiss"),
    path("<int:case_id>/", views.detail, name="detail"),
    *[
        path(f"<int:case_id>/{action.value}/", views.case_action, {"action": action}, name=action.value)
        for action in Action
    ],
]
