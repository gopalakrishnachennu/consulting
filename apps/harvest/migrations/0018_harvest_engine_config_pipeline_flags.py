"""
Add auto_backfill_jd, auto_enrich, auto_sync_to_pool toggles to HarvestEngineConfig.

These three flags control the automatic harvest funnel:
  fetch companies → JD backfill → enrich → sync to pool

All default to True so existing setups get the full funnel without any action.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0017_add_harvest_engine_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="harvestengineconfig",
            name="auto_backfill_jd",
            field=models.BooleanField(
                default=True,
                verbose_name="Auto JD backfill",
                help_text=(
                    "After each company fetch, automatically queue a description backfill "
                    "for new jobs that have no JD yet. Fires 30s after harvest."
                ),
            ),
        ),
        migrations.AddField(
            model_name="harvestengineconfig",
            name="auto_enrich",
            field=models.BooleanField(
                default=True,
                verbose_name="Auto enrich",
                help_text=(
                    "After a full batch completes, automatically run enrichment (skills, "
                    "category, experience level …) on all new unenriched jobs. "
                    "Fires 2 min after the last company task finishes."
                ),
            ),
        ),
        migrations.AddField(
            model_name="harvestengineconfig",
            name="auto_sync_to_pool",
            field=models.BooleanField(
                default=True,
                verbose_name="Auto sync to pool",
                help_text=(
                    "After enrichment, automatically promote enriched jobs with real "
                    "descriptions into the Vet Queue (Job Pool). "
                    "Fires 5 min after the last company task finishes."
                ),
            ),
        ),
    ]
