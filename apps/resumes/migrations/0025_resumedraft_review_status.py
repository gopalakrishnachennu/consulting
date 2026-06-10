from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0024_template_design_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='resumedraft',
            name='review_status',
            field=models.CharField(
                blank=True,
                choices=[
                    ('pass', 'Passed'),
                    ('review', 'Needs review'),
                    ('block', 'Blocked — fabrication risk'),
                ],
                default='pass',
                help_text='Deterministic truth-guardrail result (pass / review / block).',
                max_length=10,
            ),
        ),
    ]
