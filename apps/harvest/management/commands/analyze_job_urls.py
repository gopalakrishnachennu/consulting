"""
python manage.py analyze_job_urls

Scans all Job.original_link values and:
  1. Groups 5 sample URLs per detected platform
  2. Reports tenant_id extracted by current regex (or MISSING if no extractor yet)
  3. Lists URLs that don't match any known platform — potential new platforms to add
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from harvest.detectors import URL_PATTERNS, TENANT_EXTRACTORS, extract_tenant


class Command(BaseCommand):
    help = "Analyze job URLs to verify tenant extraction and find unknown platforms"

    def handle(self, *args, **options):
        from jobs.models import Job

        self.stdout.write("\n=== Analyzing job original_link URLs ===\n")

        platform_samples: dict[str, list[tuple[str, str]]] = defaultdict(list)
        unmatched_samples: list[str] = []
        total = 0

        for url in (
            Job.objects.exclude(original_link="")
            .values_list("original_link", flat=True)
            .iterator()
        ):
            total += 1
            url_lower = url.lower()
            matched = False
            for slug, patterns in URL_PATTERNS.items():
                for pattern in patterns:
                    if pattern in url_lower:
                        if len(platform_samples[slug]) < 5:
                            tenant = extract_tenant(slug, url)
                            platform_samples[slug].append((url, tenant))
                        matched = True
                        break
                if matched:
                    break
            if not matched and len(unmatched_samples) < 30:
                unmatched_samples.append(url)

        self.stdout.write(f"Total job URLs scanned: {total}\n")

        # ── Per-platform breakdown ────────────────────────────────────────────
        for slug in sorted(platform_samples):
            samples = platform_samples[slug]
            has_extractor = slug in TENANT_EXTRACTORS
            self.stdout.write(
                f"\n{'='*60}\n"
                f"Platform: {slug.upper()}  "
                f"({'extractor OK' if has_extractor else '⚠ NO EXTRACTOR'})\n"
                f"{'='*60}"
            )
            for url, tenant in samples:
                tenant_display = f'"{tenant}"' if tenant else "  << NO TENANT EXTRACTED"
                self.stdout.write(f"  URL   : {url}")
                self.stdout.write(f"  tenant: {tenant_display}\n")

        # ── Unmatched URLs — potential new platforms ──────────────────────────
        if unmatched_samples:
            self.stdout.write(
                f"\n{'='*60}\n"
                f"⚠  UNMATCHED URLs (no platform detected) — first {len(unmatched_samples)}\n"
                f"{'='*60}"
            )
            for url in unmatched_samples:
                self.stdout.write(f"  {url}")
        else:
            self.stdout.write("\n✓ All sampled URLs matched a known platform.\n")

        self.stdout.write("\nDone.\n")
