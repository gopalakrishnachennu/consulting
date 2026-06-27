from django.db import migrations


def enable_strict_strong_only_for_live_selective_configs(apps, schema_editor):
    HarvestEngineConfig = apps.get_model("harvest", "HarvestEngineConfig")
    HarvestEngineConfig.objects.filter(
        selective_filter_enabled=True,
        filter_audit_mode=False,
        pre_storage_filter_enabled=True,
        pre_storage_strict_strong_only=False,
    ).update(pre_storage_strict_strong_only=True)


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0069_harvestengineconfig_pre_storage_strict_strong_only"),
    ]

    operations = [
        migrations.RunPython(
            enable_strict_strong_only_for_live_selective_configs,
            migrations.RunPython.noop,
        ),
    ]
