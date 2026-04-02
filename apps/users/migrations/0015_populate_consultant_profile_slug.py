# Data migration: populate profile_slug for existing ConsultantProfile rows

from django.db import migrations, models
from django.utils.text import slugify


def populate_slugs(apps, schema_editor):
    ConsultantProfile = apps.get_model('users', 'ConsultantProfile')
    User = apps.get_model('users', 'User')
    used = set()
    for profile in ConsultantProfile.objects.all():
        user = User.objects.get(pk=profile.user_id)
        if profile.profile_slug and profile.profile_slug.strip():
            used.add(profile.profile_slug)
            continue
        full = (user.first_name or '') + ' ' + (user.last_name or '')
        full = full.strip() or user.username
        base = slugify(full or 'profile')[:60]
        if not base:
            base = slugify(user.username)[:60] or 'profile'
        slug = base
        n = 0
        while slug in used or ConsultantProfile.objects.filter(profile_slug=slug).exclude(pk=profile.pk).exists():
            n += 1
            slug = f"{base}-{n}"[:80]
        used.add(slug)
        profile.profile_slug = slug
        profile.save(update_fields=['profile_slug'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_consultant_profile_slug'),
    ]

    operations = [
        migrations.RunPython(populate_slugs, noop),
        migrations.AlterField(
            model_name='consultantprofile',
            name='profile_slug',
            field=models.SlugField(blank=True, help_text='Shareable URL slug for public profile, e.g. /c/john-doe', max_length=80, unique=True),
        ),
    ]
