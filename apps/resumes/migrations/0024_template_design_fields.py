from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0023_more_template_fonts'),
    ]

    operations = [
        migrations.AddField(
            model_name='resumetemplate',
            name='header_align',
            field=models.CharField(default='center', max_length=10),
        ),
        migrations.AddField(
            model_name='resumetemplate',
            name='skills_layout',
            field=models.CharField(default='categorized', max_length=20),
        ),
        migrations.AddField(
            model_name='resumetemplate',
            name='sections_layout',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Section order/visibility/labels: [{key,label,visible}, ...]. Empty = default.',
            ),
        ),
    ]
