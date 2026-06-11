from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('harvest', '0064_jobdomain'),
    ]

    operations = [
        migrations.AlterField(
            model_name='harvestopsrun',
            name='operation',
            field=models.CharField(
                choices=[
                    ('detect_platforms', 'Detect platforms'),
                    ('backfill_jd', 'Backfill JD'),
                    ('validate_urls', 'Validate live links'),
                    ('sync_pool', 'Sync to vet pool'),
                    ('cleanup', 'Cleanup harvested'),
                    ('classify', 'Classify raw jobs'),
                    ('classify_domains', 'Classify domains'),
                    ('llm_classify', 'LLM classify (second pass)'),
                    ('evaluate_scope', 'Evaluate RawJob scope'),
                    ('backfill_roles', 'Backfill marketing roles'),
                    ('refetch_locations', 'Refetch ambiguous locations'),
                    ('backfill_enrichment', 'Backfill enrichment'),
                    ('config_failure', 'Config read failure'),
                    ('retry_failed', 'Retry failed fetches'),
                ],
                db_index=True,
                max_length=64,
            ),
        ),
    ]
