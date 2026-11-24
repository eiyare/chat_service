"""Moodle settings moved to the workspace's Moodle integration (integrations 0002)."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("widget", "0004_moodle_student_tools"),
        ("integrations", "0002_moodle_from_widgets"),
    ]

    operations = [
        migrations.RemoveField("widget", "moodle_enabled"),
        migrations.RemoveField("widget", "moodle_shared_code_encrypted"),
        migrations.RemoveField("widget", "chunk_storage"),
        migrations.RemoveField("widget", "moodle_courses"),
        migrations.RemoveField("widget", "moodle_student_tools"),
        migrations.AlterField("widget", "greeting", models.CharField(
            default="Hi {name}! Ask me anything about our documents.", max_length=300,
            help_text="{name} becomes the visitor's name when a connected app (Moodle, your website) signs them "
                      "in, otherwise \"Guest\".")),
    ]
