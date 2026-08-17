"""Bark (iOS push) notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class BarkNotifier(BaseNotifier):
    """Send alerts to Bark (iOS push notification service)."""

    channel_type = "bark"

    def _build_payload(self, item: NewsItem) -> dict:
        title = f"\U0001f4f0 {item.title[:50]}"
        body = self._format_body(item)

        return {
            "device_key": self._get_env("device_key_env"),
            "title": title,
            "body": body,
            "url": item.url,
            "group": "News Analyzer",
        }

    def _post(self, payload: dict) -> bool:
        if not payload.get("device_key"):
            return False

        server = self.config.get("server", "https://api.day.app")
        url = f"{server}/{payload.pop('device_key')}"

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return True
        except Exception:  # noqa: BLE001
            return False

    def _format_body(self, item: NewsItem) -> str:
        parts: list[str] = []
        if item.classification:
            parts.append(
                f"\u91cd\u8981\u6027: {item.classification.importance_score:.0%}"
            )
        if item.analysis:
            parts.append(item.analysis.headline)
            parts.append(item.analysis.what_happened[:100])
        return "\n".join(parts) if parts else item.title
