"""Google News collector using RSS feed by query."""

import hashlib
from datetime import datetime, timezone
from typing import Optional

import feedparser

from app.collectors.base import BaseCollector
from app.schemas import NewsItem, SourceConfig


class GoogleNewsCollector(BaseCollector):
    """Collector for Google News RSS feeds by query."""

    BASE_URL = "https://news.google.com/rss/search"

    def fetch(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        params: dict[str, str] = {
            "q": self.config.query,
            "hl": self.config.lang,
            "gl": "US",
            "ceid": "US:en",
        }

        try:
            response = self._get(self.BASE_URL, params=params)
            response.raise_for_status()
            feed = feedparser.parse(response.text)

            for entry in feed.entries:
                link = entry.get("link", "")
                if not link:
                    continue

                url_hash = hashlib.md5(link.encode()).hexdigest()[:16]
                published = self._parse_date(entry.get("published_parsed"))

                # Extract source from title (Google News format: "Title - Source")
                title = entry.get("title", "")
                source_name = self.config.name
                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    source_name = parts[1] if len(parts) > 1 else source_name

                item = NewsItem(
                    title=title,
                    url=link,
                    source=source_name,
                    source_type="google_news",
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
            print(f"Error collecting Google News for {self.config.name}: {e}")

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
