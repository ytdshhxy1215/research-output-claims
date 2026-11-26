from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.people.access import SCHOOL_REVIEWER_GROUP, SYNC_VIEW_PERMISSION
from apps.people.models import AffiliationPeriod, Department, DepartmentReviewer, Researcher


class Command(BaseCommand):
    help = "Create synthetic teachers and reviewers; never loads research outputs directly."

    @transaction.atomic
    def handle(self, *args, **options):
        if len(settings.DEMO_PASSWORD) < 12:
            raise CommandError("Set a local DEMO_PASSWORD of at least 12 characters.")
        user_model = get_user_model()
        first, _ = Department.objects.get_or_create(code="alpha", defaults={"name": "甲院"})
        second, _ = Department.objects.get_or_create(code="beta", defaults={"name": "乙院"})

        def demo_user(username):
            marker = f"{username}@demo.invalid"
            user, created = user_model.objects.get_or_create(
                username=username, defaults={"email": marker},
            )
            if not created and (user.email != marker or user.is_staff or user.is_superuser):
                raise CommandError(f"Refusing to reuse a non-demo account: {username}.")
            if created:
                user.set_password(settings.DEMO_PASSWORD)
                user.save(update_fields=["password"])
            return user

        people = {
            "lin": ("林岚", [(first, date(2023, 1, 1), date(2026, 1, 1)),
                              (second, date(2026, 1, 1), None)]),
            "chen": ("陈星", [(second, date(2023, 1, 1), None)]),
            "lin-other": ("林岚", [(second, date(2023, 1, 1), None)]),
        }
        for username, (name, periods) in people.items():
            researcher, _ = Researcher.objects.get_or_create(
                user=demo_user(username), defaults={"name": name},
            )
            for department, starts_on, ends_on in periods:
                AffiliationPeriod.objects.get_or_create(
                    researcher=researcher, department=department, starts_on=starts_on,
                    defaults={"ends_on": ends_on},
                )
        for username, department in (("review-alpha", first), ("review-beta", second)):
            DepartmentReviewer.objects.get_or_create(user=demo_user(username), department=department)
        school, _ = Group.objects.get_or_create(name=SCHOOL_REVIEWER_GROUP)
        demo_user("school-review").groups.add(school)
        operator_group, _ = Group.objects.get_or_create(name="source-operators")
        app_label, codename = SYNC_VIEW_PERMISSION.split(".", 1)
        operator_group.permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename),
        )
        operator_group.permissions.add(
            Permission.objects.get(content_type__app_label="sources", codename="request_syncreplay"),
        )
        demo_user("source-ops").groups.add(operator_group)
        self.stdout.write(self.style.SUCCESS("Synthetic teachers and reviewers are ready."))
