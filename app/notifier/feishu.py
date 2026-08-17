"""Feishu / Lark webhook notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class FeishuNotifier(BaseNotifier):
    """Send alerts to Feishu/Lark via incoming webhook."""

    channel_type = "feishu"

    def _build_payload(self, item: NewsItem) -> dict:
        text = self._format_message(item)
        return {"msg_type": "text", "content": {"text": text}}

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

    def _format_message(self, item: NewsItem) -> str:
        lines = [f"\U0001f4f0 {item.title}"]
        if item.classification:
            lines.append(f"\u91cd\u8981\u6027: {item.classification.importance_score:.0%}")
            if item.classification.related_tickers:
                lines.append(
                    f"\u76f8\u5173\u6807\u7684: {', '.join(item.classification.related_tickers)}"
                )
        if item.analysis:
            lines.append(f"\n{item.analysis.headline}")
            lines.append(item.analysis.what_happened[:200])
            if item.analysis.actionable:
                lines.append(f"\n\U0001f4a1 {item.analysis.actionable}")
        lines.append(f"\n\U0001f517 {item.url}")
        return "\n".join(lines)
