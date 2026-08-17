"""Discord webhook notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class DiscordNotifier(BaseNotifier):
    """Send alerts to Discord via incoming webhook with embeds."""

    channel_type = "discord"

    def _build_payload(self, item: NewsItem) -> dict:
        embed: dict = {
            "title": item.title[:256],
            "url": item.url,
            "color": 0x00FF00 if item.alert_type == "analysis" else 0xFFFF00,
            "fields": [],
        }

        if item.classification:
            embed["fields"].append(
                {
                    "name": "\u91cd\u8981\u6027",
                    "value": f"{item.classification.importance_score:.0%}",
                    "inline": True,
                }
            )
            if item.classification.related_tickers:
                embed["fields"].append(
                    {
                        "name": "\u76f8\u5173\u6807\u7684",
                        "value": ", ".join(item.classification.related_tickers[:5]),
                        "inline": True,
                    }
                )

        if item.analysis:
            embed["description"] = item.analysis.what_happened[:4000]
            if item.analysis.actionable:
                embed["fields"].append(
                    {
                        "name": "\u5efa\u8bae",
                        "value": item.analysis.actionable[:1024],
                        "inline": False,
                    }
                )

        return {"embeds": [embed]}

    def _post(self, payload: dict) -> bool:
        webhook_url = self._get_env("webhook_url_env")
        if not webhook_url:
            return False

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(webhook_url, json=payload)
                response.raise_for_status()
                return True
        except Exception:  # noqa: BLE001
            return False
