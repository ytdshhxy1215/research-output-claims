import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="ReportSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True,
                                     serialize=False),
                ),
                ("captured_at", models.DateTimeField()),
                ("created_by_id", models.BigIntegerField()),
                ("policy", models.CharField(max_length=16)),
                ("policy_label", models.CharField(max_length=80)),
                ("implementation_version", models.CharField(max_length=32)),
                ("filters", models.JSONField()),
                ("payload", models.JSONField()),
            ],
            options={"ordering": ("-captured_at", "-pk")},
        ),
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION reports_reject_snapshot_mutation() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'Published report snapshots are immutable'
                        USING ERRCODE = '23000';
                END;
                $$;
                CREATE TRIGGER reports_snapshot_append_only
                BEFORE UPDATE OR DELETE ON reports_reportsnapshot
                FOR EACH ROW EXECUTE FUNCTION reports_reject_snapshot_mutation();
            """,
            reverse_sql="""
                DROP TRIGGER reports_snapshot_append_only ON reports_reportsnapshot;
                DROP FUNCTION reports_reject_snapshot_mutation();
            """,
        ),
    ]
