from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_consultantprofile_match_jd_title_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='timezone',
            field=models.CharField(
                max_length=50,
                default='UTC',
                help_text="Time zone used for calendars, scheduling, and notifications (e.g. 'Europe/London', 'Asia/Kolkata').",
            ),
        ),
    ]

