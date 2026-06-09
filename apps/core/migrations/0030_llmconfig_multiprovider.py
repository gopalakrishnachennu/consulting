from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_error_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='llmconfig',
            name='provider',
            field=models.CharField(
                choices=[
                    ('openai', 'OpenAI'),
                    ('deepseek', 'DeepSeek'),
                    ('openrouter', 'OpenRouter (any model)'),
                    ('together', 'Together AI'),
                    ('custom', 'Custom (OpenAI-compatible)'),
                ],
                default='openai',
                help_text='LLM provider. All are OpenAI-API-compatible.',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='llmconfig',
            name='base_url',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'OpenAI-compatible API base URL. Blank = use the provider default '
                    '(DeepSeek: https://api.deepseek.com · OpenRouter: https://openrouter.ai/api/v1).'
                ),
                max_length=300,
            ),
        ),
        migrations.AddField(
            model_name='llmconfig',
            name='validation_model',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Cheaper model for validation / classification (e.g. gpt-4o-mini, deepseek-chat). '
                    'Blank = use the generation model.'
                ),
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name='llmconfig',
            name='encrypted_api_key',
            field=models.TextField(blank=True, help_text='Encrypted API key for the selected provider'),
        ),
        migrations.AlterField(
            model_name='llmconfig',
            name='active_model',
            field=models.CharField(
                default='gpt-4o-mini',
                help_text='Model for resume generation (e.g. gpt-4o, deepseek-chat, openai/gpt-4o via OpenRouter).',
                max_length=100,
            ),
        ),
    ]
