from __future__ import annotations

from typing import Protocol

from maui_market.models import ComplexConfig, Listing


class Scraper(Protocol):
    def scrape(self, config: ComplexConfig) -> list[Listing]:
        """Return active listings for the configured complex."""
