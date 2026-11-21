from django.conf import settings
from django.db import migrations, models

HELP = ("Added to the system prompt when the assistant can look up a Moodle student's own grades, completion "
        "and deadlines (website widget > Student data tools).")


def seed(apps, schema_editor):
    apps.get_model("siteconfig", "PlatformSettings").objects.update(student_tools_prompt=settings.RAG_STUDENT_TOOLS_PROMPT)


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0010_provider_retries")]

    operations = [
        migrations.AddField(
            "platformsettings", "student_tools_prompt",
            models.TextField(default="", verbose_name="Instructions: Moodle student tools", help_text=HELP),
            preserve_default=False,
        ),
        migrations.AddField(
            "workspacesettings", "student_tools_prompt",
            models.TextField(blank=True, verbose_name="Instructions: Moodle student tools", help_text=HELP),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
