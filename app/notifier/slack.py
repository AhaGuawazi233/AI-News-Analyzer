"""Slack webhook notifier with Block Kit format."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class SlackNotifier(BaseNotifier):
    """Send alerts to Slack via incoming webhook using Block Kit."""

    channel_type = "slack"

    def _build_payload(self, item: NewsItem) -> dict:
        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"\U0001f4f0 {item.title[:150]}",
                },
            }
        ]

        if item.classification:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*\u91cd\u8981\u6027:* {item.classification.importance_score:.0%}",
                    },
                }
            )
            if item.classification.related_tickers:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*\u6807\u7684:* {', '.join(item.classification.related_tickers)}",
                        },
                    }
                )

        if item.analysis:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{item.analysis.headline}*\n{item.analysis.what_happened[:300]}",
                    },
                }
            )
            if item.analysis.actionable:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*\U0001f4a1 \u5efa\u8bae:* {item.analysis.actionable}",
                        },
                    }
                )

        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "\u67e5\u770b\u539f\u6587",
                        },
                        "url": item.url,
                    }
                ],
            }
        )

        return {"blocks": blocks}

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
