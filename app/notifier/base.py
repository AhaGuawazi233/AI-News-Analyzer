"""Abstract notification base class and multi-channel dispatcher.

Unified filtering logic lives here; subclasses only implement payload format
and the actual HTTP post.  ``NotifierDispatcher`` fans out to all enabled
channels in parallel via a thread pool so a single slow/broken channel
cannot block the others.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from app.schemas import NewsItem


class BaseNotifier(ABC):
    """Abstract notification base class.

    Unified filtering logic - subclasses only implement payload format and posting.
    """

    channel_type: str  # Must be set by subclass

    def __init__(
        self,
        config: dict,
        alert_threshold: float = 0.75,
        brief_alert_threshold: float = 0.85,
    ) -> None:
        self.config = config
        self.alert_threshold = alert_threshold
        self.brief_alert_threshold = brief_alert_threshold

    def send(self, item: NewsItem) -> bool:
        """Unified entry: check should_send -> build payload -> post.

        Returns True if notification was actually sent.
        """
        if not self._should_send(item):
            return False

        try:
            payload = self._build_payload(item)
            return self._post(payload)
        except Exception as e:  # noqa: BLE001
            # Single channel failure must not affect others
            print(f"Notification failed for {self.channel_type}: {e}")
            return False

    def _should_send(self, item: NewsItem) -> bool:
        """Filter by alert_type + importance_score."""
        if not item.classification:
            return False

        importance = item.classification.importance_score

        if item.alert_type == "analysis":
            return importance >= self.alert_threshold
        elif item.alert_type == "brief":
            return importance >= self.brief_alert_threshold

        return False

    def _get_env(self, key: str) -> Optional[str]:
        """Get env var from config key name."""
        env_key = self.config.get(key)
        if env_key:
            return os.getenv(env_key)
        return None

    @abstractmethod
    def _build_payload(self, item: NewsItem) -> dict:
        """Build channel-specific payload from NewsItem."""
        ...

    @abstractmethod
    def _post(self, payload: dict) -> bool:
        """Send to channel. Returns False on failure, never raises."""
        ...


class NotifierDispatcher:
    """Multi-channel parallel dispatcher.

    Sends to all enabled channels in parallel using thread pool.
    """

    def __init__(self, channels: list[BaseNotifier]) -> None:
        self.channels = channels

    def dispatch(self, item: NewsItem) -> dict[str, bool]:
        """Parallel send to all enabled channels.

        Returns {channel_type: success_bool}
        """
        results: dict[str, bool] = {}

        if not self.channels:
            return results

        # Use thread pool for parallel sending
        with ThreadPoolExecutor(max_workers=min(len(self.channels), 8)) as executor:
            futures = {
                executor.submit(channel.send, item): channel.channel_type
                for channel in self.channels
            }

            for future in as_completed(futures):
                channel_type = futures[future]
                try:
                    results[channel_type] = future.result(timeout=30)
                except Exception:  # noqa: BLE001
                    results[channel_type] = False

        return results
