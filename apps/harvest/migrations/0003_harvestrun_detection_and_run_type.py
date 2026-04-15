# Generated manually for platform detection runs + run type

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0002_seed_platforms"),
    ]

    operations = [
        migrations.AddField(
            model_name="harvestrun",
            name="detection_detected",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Companies with a detected platform (detection runs only).",
            ),
        ),
        migrations.AddField(
            model_name="harvestrun",
            name="detection_total",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Companies processed in this detection run.",
            ),
        ),
        migrations.AddField(
            model_name="harvestrun",
            name="run_type",
            field=models.CharField(
                choices=[
                    ("HARVEST", "Harvest"),
                    ("DETECTION", "Platform detection"),
                ],
                default="HARVEST",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="harvestrun",
            name="platform",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="harvest_runs",
                to="harvest.jobboardplatform",
            ),
        ),
    ]
