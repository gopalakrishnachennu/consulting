from .workday import WorkdayHarvester
from .greenhouse import GreenhouseHarvester
from .lever import LeverHarvester
from .ashby import AshbyHarvester
from .icims import IcimsHarvester
from .jobvite import JobviteHarvester
from .taleo import TaleoHarvester
from .html_scraper import HTMLScrapeHarvester

HARVESTER_MAP: dict[str, type] = {
    "workday": WorkdayHarvester,
    "greenhouse": GreenhouseHarvester,
    "lever": LeverHarvester,
    "ashby": AshbyHarvester,
    "icims": IcimsHarvester,
    "jobvite": JobviteHarvester,
    "taleo": TaleoHarvester,
}


def get_harvester(platform_slug: str):
    """Return the appropriate harvester instance for a platform slug."""
    cls = HARVESTER_MAP.get(platform_slug, HTMLScrapeHarvester)
    return cls()


__all__ = [
    "WorkdayHarvester", "GreenhouseHarvester", "LeverHarvester",
    "AshbyHarvester", "IcimsHarvester", "JobviteHarvester", "TaleoHarvester",
    "HTMLScrapeHarvester", "get_harvester", "HARVESTER_MAP",
]
