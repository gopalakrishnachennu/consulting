from django.db import migrations


def enforce_strong_only_policy(apps, schema_editor):
    HarvestEngineConfig = apps.get_model("harvest", "HarvestEngineConfig")
    VetGateConfig = apps.get_model("harvest", "VetGateConfig")

    HarvestEngineConfig.objects.filter(selective_filter_enabled=True).update(
        filter_audit_mode=False,
        pre_storage_filter_enabled=True,
        pre_storage_strict_strong_only=True,
        filter_full_crawl=True,
    )
    VetGateConfig.objects.all().update(allow_possible_filter=False)


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0071_harvestengineconfig_obscura_binary_path_and_more"),
    ]

    operations = [
        migrations.RunPython(
            enforce_strong_only_policy,
            migrations.RunPython.noop,
        ),
    ]
