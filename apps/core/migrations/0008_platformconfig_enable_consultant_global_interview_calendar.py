from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_platformconfig_match_jd_title_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformconfig',
            name='enable_consultant_global_interview_calendar',
            field=models.BooleanField(
                default=False,
                help_text='If enabled, consultants can view the full interview calendar (all candidates) instead of only their own.',
            ),
        ),
    ]

