from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0004_seed_new_platforms"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyplatformlabel",
            name="portal_alive",
            field=models.BooleanField(
                blank=True,
                null=True,
                help_text="True=HTTP 2xx/3xx, False=4xx/5xx/timeout, None=not yet checked.",
            ),
        ),
        migrations.AddField(
            model_name="companyplatformlabel",
            name="portal_last_verified",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When the portal URL was last HTTP-checked.",
            ),
        ),
    ]
