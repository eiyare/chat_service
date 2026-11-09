"""Create the platform settings row from the current Django settings (.env),
so existing behaviour is unchanged and from now on it is managed in the admin."""
from django.db import migrations


def seed(apps, schema_editor):
    from siteconfig.spec import SPECS

    PlatformSettings = apps.get_model("siteconfig", "PlatformSettings")
    if not PlatformSettings.objects.filter(pk=1).exists():
        # Only the fields that exist at this point in the migration history; later
        # migrations add theirs with defaults.
        existing = {f.name for f in PlatformSettings._meta.get_fields()}
        PlatformSettings.objects.create(pk=1, **{s.name: s.default() for s in SPECS if s.name in existing})


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
