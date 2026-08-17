"""RSS feed collector for standard feeds (Reuters, BBC, etc.)."""

import hashlib
from datetime import datetime, timezone
from typing import Optional

import feedparser

from app.collectors.base import BaseCollector
from app.schemas import NewsItem, SourceConfig


class RSSCollector(BaseCollector):
    """Collector for standard RSS feeds (Reuters, BBC, etc.)."""

    def fetch(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            response = self._get(self.config.url)
            response.raise_for_status()
            feed = feedparser.parse(response.text)

            for entry in feed.entries:
                url = entry.get("link", "")
                if not url:
                    continue

                url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
                published = self._parse_date(entry.get("published_parsed"))

                item = NewsItem(
                    title=entry.get("title", ""),
                    url=url,
                    source=self.config.name,
                    source_type="rss",
                    published=published,
                    summary=entry.get("summary", ""),
                    lang=self.config.lang,
                    collected_at=datetime.now(timezone.utc).isoformat(),
                    url_hash=url_hash,
                    status="collected",
                )
                items.append(item)
        except Exception as e:
            # Single source failure isolation — don't crash the whole collection
            print(f"Error collecting RSS from {self.config.name}: {e}")

        return items

    def _parse_date(self, date_parsed: object) -> Optional[str]:
        """Convert feedparser time struct to ISO 8601 string."""
        if date_parsed:
            try:
                dt = datetime(*date_parsed[:6], tzinfo=timezone.utc)  # type: ignore[index]
                return dt.isoformat()
            except Exception:
                return None
        return None
