from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0005_company_career_site_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="domain",
            field=models.CharField(max_length=255, blank=True, db_index=True),
        ),
    ]

