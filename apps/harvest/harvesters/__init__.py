from .workday import WorkdayHarvester
from .greenhouse import GreenhouseHarvester
from .lever import LeverHarvester
from .ashby import AshbyHarvester
from .html_scraper import HTMLScrapeHarvester

HARVESTER_MAP: dict[str, type] = {
    "workday": WorkdayHarvester,
    "greenhouse": GreenhouseHarvester,
    "lever": LeverHarvester,
    "ashby": AshbyHarvester,
}


def get_harvester(platform_slug: str):
    """Return the appropriate harvester instance for a platform slug."""
    cls = HARVESTER_MAP.get(platform_slug, HTMLScrapeHarvester)
    return cls()


__all__ = [
    "WorkdayHarvester", "GreenhouseHarvester", "LeverHarvester",
    "AshbyHarvester", "HTMLScrapeHarvester", "get_harvester", "HARVESTER_MAP",
]
