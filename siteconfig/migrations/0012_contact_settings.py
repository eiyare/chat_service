from django.conf import settings
from django.db import migrations, models

HELP = ("Where \"Book a demo\" and \"Contact us\" enquiries from the public site are emailed (replies go straight "
        "to the sender). Every enquiry is also kept under Portal > Enquiries.")


def seed(apps, schema_editor):
    # HELPDESK_EMAIL in .env was briefly supported; the address is set in Django admin now.
    apps.get_model("siteconfig", "PlatformSettings").objects.update(
        helpdesk_email=getattr(settings, "HELPDESK_EMAIL", "") or "")


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0011_student_tools_prompt")]

    operations = [
        migrations.AddField(
            "platformsettings", "helpdesk_email",
            models.CharField(blank=True, default="", max_length=500, verbose_name="Helpdesk email", help_text=HELP),
            preserve_default=False,
        ),
        migrations.AddField(
            "platformsettings", "show_helpdesk_email",
            models.BooleanField(default=False, verbose_name="Show the helpdesk email on the contact page", help_text=(
                "Also show the address on the contact page, for people who prefer to write directly. Public "
                "addresses attract spam.")),
            preserve_default=False,
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
