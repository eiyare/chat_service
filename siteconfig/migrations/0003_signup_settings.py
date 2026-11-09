from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0002_seed_platform_settings")]

    operations = [
        migrations.AddField(
            model_name="platformsettings", name="allow_signup",
            field=models.BooleanField(default=True, help_text="Anyone can create an account from the home page and create free chatbots.", verbose_name="Allow sign-up"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="platformsettings", name="free_chatbots_per_account",
            field=models.IntegerField(default=1, help_text="Workspaces an account may create itself. Superusers are not limited. Free workspaces use the Workspace defaults limits above.", validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)], verbose_name="Free chatbots per account"),
            preserve_default=False,
        ),
    ]
