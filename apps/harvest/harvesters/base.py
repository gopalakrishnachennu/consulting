from abc import ABC, abstractmethod
from typing import Any

import requests


class BaseHarvester(ABC):
    """Abstract base for platform-specific job harvesters."""

    platform_slug: str = ""

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

    @abstractmethod
    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        """Fetch jobs for a company. Returns list of raw job dicts."""
        raise NotImplementedError

    def _get(self, url: str, params: dict | None = None, timeout: int = 15) -> dict | list:
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    def _post(self, url: str, json_data: dict, timeout: int = 15) -> dict | list:
        headers = {**self.headers, "Content-Type": "application/json"}
        try:
            resp = requests.post(url, json=json_data, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"error": str(e)}
