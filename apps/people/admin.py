from django.contrib import admin

from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(Researcher)
class ResearcherAdmin(admin.ModelAdmin):
    list_display = ("name", "user")
    search_fields = ("name", "user__username")


@admin.register(AffiliationPeriod)
class AffiliationAdmin(admin.ModelAdmin):
    list_display = ("researcher", "department", "starts_on", "ends_on", "is_primary")
    list_filter = ("department", "is_primary")


@admin.register(DepartmentReviewer)
class ReviewerAdmin(admin.ModelAdmin):
    list_display = ("user", "department")
    list_filter = ("department",)
