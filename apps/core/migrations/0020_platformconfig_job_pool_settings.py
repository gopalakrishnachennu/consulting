from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_remove_llmconfig_active_prompt'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformconfig',
            name='require_pool_staging',
            field=models.BooleanField(
                default=True,
                help_text=(
                    'When enabled (recommended), all new jobs land in the Pool for review before going live. '
                    'Disable to make new jobs OPEN immediately (legacy behaviour).'
                ),
            ),
        ),
        migrations.AddField(
            model_name='platformconfig',
            name='auto_approve_pool_threshold',
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    'Validation score threshold (0–100). Jobs that score at or above this number are '
                    'automatically promoted from Pool to Open without manual review. '
                    'Set to 0 to disable auto-approval entirely.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='platformconfig',
            name='pool_review_notify_emails',
            field=models.TextField(
                blank=True,
                help_text=(
                    'Comma-separated list of email addresses to notify whenever a new job enters the Pool. '
                    'Leave blank to send no external emails. '
                    'Example: admin@company.com, recruiter@company.com'
                ),
            ),
        ),
    ]
