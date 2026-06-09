from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resumes', '0022_resumeeditorstate_parser_version'),
    ]

    operations = [
        migrations.AlterField(
            model_name='resumetemplate',
            name='font_family',
            field=models.CharField(
                choices=[
                    ('Georgia, serif', 'Georgia (Serif)'),
                    ('"Times New Roman", serif', 'Times New Roman'),
                    ('Garamond, Georgia, serif', 'Garamond'),
                    ('"Palatino Linotype", serif', 'Palatino'),
                    ('Arial, sans-serif', 'Arial'),
                    ('"Helvetica Neue", Arial, sans-serif', 'Helvetica Neue'),
                    ('Calibri, Arial, sans-serif', 'Calibri'),
                    ('"Trebuchet MS", sans-serif', 'Trebuchet MS'),
                    ('"Open Sans", sans-serif', 'Open Sans'),
                    ('Inter, "Helvetica Neue", Arial, sans-serif', 'Inter (Modern)'),
                    ('Lato, Arial, sans-serif', 'Lato (Modern)'),
                    ('Roboto, Arial, sans-serif', 'Roboto (Modern)'),
                    ('Cambria, Georgia, serif', 'Cambria'),
                    ('Verdana, sans-serif', 'Verdana'),
                ],
                default='Georgia, serif',
                max_length=120,
            ),
        ),
    ]
