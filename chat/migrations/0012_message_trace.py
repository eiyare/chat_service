from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0011_message_error")]

    operations = [
        migrations.AddField(
            "message", "trace",
            models.JSONField(blank=True, default=list,
                             help_text="Brain map: each step that produced this reply (see chat/trace.py)"),
        ),
    ]
