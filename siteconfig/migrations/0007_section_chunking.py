from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("siteconfig", "0006_rewriting_and_simpler_prompt")]

    operations = [
        migrations.AddField(
            "platformsettings", "section_chunking",
            models.BooleanField(default=True, verbose_name="Section-aware chunking", help_text=(
                "Split documents at their headings (\"1. Opening Hours\") so chunks don't mix topics, and add the "
                "document title and section to each chunk. Applies to documents (re)built afterwards.")),
            preserve_default=False,
        ),
    ]
