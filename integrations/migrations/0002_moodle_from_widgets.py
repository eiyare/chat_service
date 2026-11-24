"""Each workspace's Moodle settings (on its widget until now) become a Moodle integration,
with the same shared code, so the installed plugin keeps working unchanged."""
import secrets

from django.db import migrations


def forwards(apps, schema_editor):
    Widget = apps.get_model("widget", "Widget")
    Integration = apps.get_model("integrations", "Integration")
    Document = apps.get_model("rag", "Document")
    with_courses = set(Document.objects.filter(moodle_course_id__isnull=False).values_list("tenant_id", flat=True))
    for widget in Widget.objects.all():
        if not (widget.moodle_enabled or widget.moodle_shared_code_encrypted or widget.tenant_id in with_courses):
            continue
        Integration.objects.create(
            tenant_id=widget.tenant_id, kind="moodle", name="Moodle", key=secrets.token_urlsafe(12),
            secret_encrypted=widget.moodle_shared_code_encrypted, enabled=widget.moodle_enabled, signed_users=True,
            content=widget.moodle_courses, tools=widget.moodle_student_tools,
            share_documents=widget.chunk_storage == "mirror", manifest={},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0001_initial"),
        ("widget", "0004_moodle_student_tools"),
        ("rag", "0007_moodle_courses"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
