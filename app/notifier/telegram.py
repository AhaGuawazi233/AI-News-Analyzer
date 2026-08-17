"""Telegram Bot API notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class TelegramNotifier(BaseNotifier):
    """Send alerts via Telegram Bot API sendMessage."""

    channel_type = "telegram"

    def _build_payload(self, item: NewsItem) -> dict:
        text = self._format_message(item)
        return {
            "chat_id": self._get_env("chat_id_env"),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    def _post(self, payload: dict) -> bool:
        bot_token = self._get_env("bot_token_env")
        if not bot_token or not payload.get("chat_id"):
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return True
        except Exception:  # noqa: BLE001
            return False

    def _format_message(self, item: NewsItem) -> str:
        lines = [f"<b>\U0001f4f0 {item.title}</b>"]
        if item.classification:
            lines.append(
                f"\u91cd\u8981\u6027: {item.classification.importance_score:.0%}"
            )
            if item.classification.related_tickers:
                lines.append(
                    f"\u6807\u7684: {', '.join(item.classification.related_tickers)}"
                )
        if item.analysis:
            lines.append(f"\n<b>{item.analysis.headline}</b>")
            lines.append(item.analysis.what_happened[:200])
            if item.analysis.actionable:
                lines.append(f"\n\U0001f4a1 {item.analysis.actionable}")
        lines.append(f'\n\U0001f517 <a href="{item.url}">\u539f\u6587</a>')
        return "\n".join(lines)
