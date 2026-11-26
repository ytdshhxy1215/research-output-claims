from django.db import migrations


def install_append_only_audit(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        CREATE FUNCTION claims_reject_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Claim events are append-only' USING ERRCODE = '23514';
        END;
        $$
    """)
    schema_editor.execute("""
        CREATE TRIGGER claims_event_append_only
        BEFORE UPDATE OR DELETE ON claims_claimevent
        FOR EACH ROW EXECUTE FUNCTION claims_reject_event_mutation()
    """)


def remove_append_only_audit(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP TRIGGER claims_event_append_only ON claims_claimevent")
    schema_editor.execute("DROP FUNCTION claims_reject_event_mutation()")


class Migration(migrations.Migration):
    dependencies = [("claims", "0001_initial")]
    operations = [migrations.RunPython(install_append_only_audit, remove_append_only_audit)]
