from __future__ import annotations

from django.core.management.base import BaseCommand

from harvest.models import HarvestEngineConfig


class Command(BaseCommand):
    help = "Set the remote-jobs location policy on HarvestEngineConfig (singleton)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--policy",
            choices=["review", "us", "target", "cold"],
            required=True,
            help="remote_unknown_policy value for remote jobs with no resolvable country.",
        )
        parser.add_argument(
            "--llm-scan",
            choices=["on", "off"],
            default=None,
            help="Toggle remote_llm_jd_scan (LLM reads the JD for a location first).",
        )

    def handle(self, *args, **options):
        cfg = HarvestEngineConfig.get()
        fields = ["remote_unknown_policy"]
        cfg.remote_unknown_policy = options["policy"]
        if options["llm_scan"] is not None:
            cfg.remote_llm_jd_scan = options["llm_scan"] == "on"
            fields.append("remote_llm_jd_scan")
        cfg.save(update_fields=fields)
        self.stdout.write(self.style.SUCCESS(
            f"Updated: remote_unknown_policy={cfg.remote_unknown_policy} "
            f"remote_llm_jd_scan={cfg.remote_llm_jd_scan}"
        ))
