from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0076_alter_cleanup_inactive_age_days_helptext"),
    ]

    operations = [
        migrations.AddField(
            model_name="vetgateconfig",
            name="vet_queue_country_codes",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='ISO codes allowed in the vetting (POOL) queue, e.g. ["US"]. When empty, uses Harvest Engine target countries.',
                verbose_name="Vet queue country codes",
            ),
        ),
    ]
