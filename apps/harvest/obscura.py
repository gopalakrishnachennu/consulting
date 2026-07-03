from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

from .runtime_config import get_harvest_engine_config

logger = logging.getLogger(__name__)

VALID_WAIT_UNTIL = {"load", "domcontentloaded", "networkidle0"}


@dataclass(frozen=True)
class ObscuraSettings:
    enabled: bool
    binary_path: str
    timeout_secs: int
    wait_until: str
    stealth: bool


def load_obscura_settings() -> ObscuraSettings:
    cfg = get_harvest_engine_config("obscura_settings")

    enabled = bool(getattr(cfg, "obscura_enabled", False)) if cfg is not None else False
    binary_path = (
        str(getattr(cfg, "obscura_binary_path", "") or "").strip()
        if cfg is not None
        else ""
    ) or os.getenv("OBSCURA_BINARY_PATH", "obscura")
    timeout_secs = int(getattr(cfg, "obscura_timeout_secs", 20) or 20) if cfg is not None else 20
    wait_until = (
        str(getattr(cfg, "obscura_wait_until", "") or "").strip().lower()
        if cfg is not None
        else "networkidle0"
    )
    if wait_until not in VALID_WAIT_UNTIL:
        wait_until = "networkidle0"
    stealth = bool(getattr(cfg, "obscura_stealth", False)) if cfg is not None else False

    return ObscuraSettings(
        enabled=enabled,
        binary_path=binary_path,
        timeout_secs=max(5, min(timeout_secs, 120)),
        wait_until=wait_until,
        stealth=stealth,
    )


def resolve_obscura_binary(binary_path: str) -> str | None:
    binary_path = (binary_path or "").strip()
    if not binary_path:
        return None
    if os.path.isabs(binary_path):
        return binary_path if os.path.exists(binary_path) else None
    return shutil.which(binary_path)


def obscura_binary_available(binary_path: str | None = None) -> bool:
    settings = load_obscura_settings()
    return resolve_obscura_binary(binary_path or settings.binary_path) is not None


def build_obscura_fetch_command(url: str, settings: ObscuraSettings) -> list[str]:
    binary = resolve_obscura_binary(settings.binary_path) or settings.binary_path
    cmd = [binary]
    if settings.stealth:
        cmd.append("--stealth")
    cmd.extend(
        [
            "fetch",
            url,
            "--dump",
            "html",
            "--wait-until",
            settings.wait_until,
            "--timeout",
            str(settings.timeout_secs),
            "--quiet",
        ]
    )
    return cmd


def fetch_html_with_obscura(url: str, *, settings: ObscuraSettings | None = None) -> str:
    settings = settings or load_obscura_settings()
    if not settings.enabled:
        raise RuntimeError("Obscura renderer is disabled in Harvest Engine Config.")

    binary = resolve_obscura_binary(settings.binary_path)
    if not binary:
        raise FileNotFoundError(
            f"Obscura binary not found: {settings.binary_path}. "
            "Install it in the image/worker or update the configured path."
        )

    cmd = build_obscura_fetch_command(url, settings)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=settings.timeout_secs + 5,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Obscura timed out after {settings.timeout_secs}s for {url}"
        ) from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise RuntimeError(f"Obscura fetch failed for {url}: {detail[:500]}")

    rendered = proc.stdout.decode("utf-8", errors="replace")
    if not rendered.strip():
        raise RuntimeError(f"Obscura returned empty HTML for {url}")
    return rendered


def record_obscura_failure(
    *,
    url: str,
    backend_mode: str,
    exc: Exception,
    fallback_used: bool,
) -> None:
    """Persist a deduplicated ops audit row for Obscura runtime failures."""
    try:
        from django.core.cache import cache
        from django.utils import timezone

        from .models import HarvestOpsRun

        host = (urlparse(url).netloc or "")[:120]
        error_type = type(exc).__name__
        cache_key = f"harvest:obscura-failure:{backend_mode}:{host}:{error_type}"
        if not cache.add(cache_key, True, timeout=300):
            return

        HarvestOpsRun.objects.create(
            operation=HarvestOpsRun.Operation.CONFIG_FAILURE,
            status=(
                HarvestOpsRun.Status.PARTIAL
                if fallback_used
                else HarvestOpsRun.Status.FAILED
            ),
            finished_at=timezone.now(),
            progress_message="Obscura renderer failure",
            audit_payload={
                "completion": {
                    "component": "obscura",
                    "backend_mode": backend_mode,
                    "fallback_used": fallback_used,
                    "host": host,
                    "error_type": error_type,
                    "error": str(exc)[:500],
                }
            },
        )
    except Exception:
        logger.exception("Failed to persist Obscura failure audit for %s", url)
