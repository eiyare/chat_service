from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0010_search_query")]

    operations = [
        migrations.AddField(
            "message", "error",
            models.CharField(blank=True, max_length=500,
                             help_text="Why the model call failed: kind, attempts and the provider's error"),
        ),
    ]
