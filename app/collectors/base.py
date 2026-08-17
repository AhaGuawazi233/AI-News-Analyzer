"""Abstract base collector with proxy support."""

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from app.net_safety import safe_get
from app.schemas import NewsItem, SourceConfig


class BaseCollector(ABC):
    """Base class for all news collectors with proxy support."""

    def __init__(
        self,
        config: SourceConfig,
        timeout: int = 20,
        default_proxy: Optional[str] = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.proxy = config.proxy or default_proxy

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Fetch a source URL with public-network and redirect validation."""
        return safe_get(
            url,
            timeout=self.timeout,
            proxy=self.proxy,
            params=params,
        )

    @abstractmethod
    def fetch(self) -> list[NewsItem]:
        """Fetch news items from source. Returns list of NewsItem."""
        ...
