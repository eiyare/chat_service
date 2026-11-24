"""Moodle course content becomes content of the workspace's Moodle integration, scope
"course:<id>", keeping its passages (no reindexing)."""
import django.db.models.deletion
from django.db import migrations, models


def forwards(apps, schema_editor):
    Document = apps.get_model("rag", "Document")
    Integration = apps.get_model("integrations", "Integration")
    moodle = dict(Integration.objects.filter(kind="moodle").values_list("tenant_id", "pk"))
    for doc in Document.objects.filter(moodle_course_id__isnull=False).only(
            "pk", "tenant_id", "moodle_course_id", "moodle_key", "moodle_version"):
        Document.objects.filter(pk=doc.pk).update(
            integration_id=moodle[doc.tenant_id], scope=f"course:{doc.moodle_course_id}",
            item_key=doc.moodle_key, item_version=doc.moodle_version)


class Migration(migrations.Migration):
    dependencies = [
        ("rag", "0007_moodle_courses"),
        ("integrations", "0002_moodle_from_widgets"),
    ]

    operations = [
        migrations.AddField("document", "integration", models.ForeignKey(
            blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="documents",
            to="integrations.integration", help_text="The app that sent it; empty for uploaded documents")),
        migrations.AddField("document", "scope", models.CharField(
            blank=True, db_index=True, max_length=100,
            help_text='e.g. "course:12"; blank = shared with the whole workspace')),
        migrations.AddField("document", "item_key", models.CharField(
            blank=True, max_length=100, help_text="The app's ID for the item, e.g. cm:42")),
        migrations.AddField("document", "item_version", models.CharField(
            blank=True, max_length=64, help_text="Changes when the item does in the app")),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveConstraint("document", "rag_unique_document_per_tenant"),
        migrations.RemoveConstraint("document", "rag_unique_moodle_item"),
        migrations.RemoveField("document", "moodle_course_id"),
        migrations.RemoveField("document", "moodle_key"),
        migrations.RemoveField("document", "moodle_version"),
        migrations.AddConstraint("document", models.UniqueConstraint(
            condition=models.Q(integration__isnull=True), fields=("tenant", "sha256"),
            name="rag_unique_document_per_tenant")),
        migrations.AddConstraint("document", models.UniqueConstraint(
            condition=models.Q(integration__isnull=False), fields=("integration", "scope", "item_key"),
            name="rag_unique_app_item")),
    ]
