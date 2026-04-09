from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_messaging_thread_org_and_message_fields'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='llmconfig',
            name='active_prompt',
        ),
    ]
