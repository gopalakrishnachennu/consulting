from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0004_blacklist_dnd"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="career_site_url",
            field=models.URLField(
                blank=True,
                help_text="Careers / jobs page URL for this company.",
            ),
        ),
    ]

