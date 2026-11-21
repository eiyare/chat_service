import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0009_alter_platformsettings_confident_factor")]

    operations = [
        migrations.AddField(
            "platformsettings", "provider_retries",
            models.IntegerField(
                default=2, verbose_name="Retries after a provider error",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(5)],
                help_text=(
                    "When the model provider fails with a temporary error (busy/overloaded, rate limited, timed out, "
                    "unreachable) before any of the reply has been shown, try again this many times, waiting about "
                    "1, 2, 4 ... seconds in between. 0 shows the error straight away.")),
            preserve_default=False,
        ),
    ]
