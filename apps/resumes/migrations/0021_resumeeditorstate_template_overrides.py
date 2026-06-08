from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0020_masterprompt_system_prompt_optional'),
    ]

    operations = [
        migrations.AddField(
            model_name='resumeeditorstate',
            name='template_overrides',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Per-draft template tweaks from the Customise panel (font, sizes, "
                    "colors, margins...). Merged over the selected template for both "
                    "preview and export, so the live preview matches the downloaded file."
                ),
            ),
        ),
    ]
