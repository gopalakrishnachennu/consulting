from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0019_pipeline_v3_models'),
    ]

    operations = [
        migrations.AlterField(
            model_name='masterprompt',
            name='system_prompt',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Legacy/unused: the active pipeline uses a fixed system message and '
                    'reads only generation_rules. Kept for the legacy single-call path and '
                    'history.'
                ),
            ),
        ),
    ]
