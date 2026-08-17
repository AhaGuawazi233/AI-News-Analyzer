"""Enterprise WeChat (WeCom) webhook notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class WeComNotifier(BaseNotifier):
    """Send alerts to WeCom via incoming webhook."""

    channel_type = "wecom"

    def _build_payload(self, item: NewsItem) -> dict:
        text = self._format_message(item)
        return {"msgtype": "text", "text": {"content": text}}

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
            lines.append(
                f"\u91cd\u8981\u6027: {item.classification.importance_score:.0%}"
            )
        if item.analysis:
            lines.append(f"\n{item.analysis.headline}")
            lines.append(item.analysis.what_happened[:200])
        lines.append(f"\n\U0001f517 {item.url}")
        return "\n".join(lines)
