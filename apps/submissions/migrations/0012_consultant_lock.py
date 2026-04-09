from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ('submissions', '0011_phase4_5'),
        ('users', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsultantLock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('locked_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('last_heartbeat_at', models.DateTimeField(auto_now_add=True)),
                ('consultant', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='active_lock',
                    to='users.consultantprofile',
                )),
                ('locked_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='held_consultant_locks',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Consultant Lock',
                'verbose_name_plural': 'Consultant Locks',
            },
        ),
    ]
