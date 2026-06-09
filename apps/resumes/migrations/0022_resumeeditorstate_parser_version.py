from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0021_resumeeditorstate_template_overrides'),
    ]

    operations = [
        migrations.AddField(
            model_name='resumeeditorstate',
            name='parser_version',
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    'Parser version that produced sections_json. When the parser is upgraded, '
                    'the editor re-parses stale states automatically.'
                ),
            ),
        ),
    ]
