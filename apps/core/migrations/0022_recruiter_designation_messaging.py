# Generated manually — ensure default Recruiter designation includes messaging.

from django.db import migrations


def add_messaging_to_recruiter(apps, schema_editor):
    FeatureFlag = apps.get_model('core', 'FeatureFlag')
    EmployeeDesignation = apps.get_model('core', 'EmployeeDesignation')
    ff = FeatureFlag.objects.filter(key='consultant_messaging').first()
    des = EmployeeDesignation.objects.filter(slug='recruiter').first()
    if ff and des:
        des.allowed_features.add(ff)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_feature_flags_and_designations'),
    ]

    operations = [
        migrations.RunPython(add_messaging_to_recruiter, noop),
    ]
