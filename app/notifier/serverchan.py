"""Server酱 (ServerChan) WeChat notifier."""

from __future__ import annotations

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class ServerChanNotifier(BaseNotifier):
    """Send alerts via Server酱 (sct.ftqq.com) to WeChat."""

    channel_type = "serverchan"

    def _build_payload(self, item: NewsItem) -> dict:
        title = f"\U0001f4f0 {item.title[:32]}"
        desp = self._format_desp(item)

        return {"title": title, "desp": desp}

    def _post(self, payload: dict) -> bool:
        sendkey = self._get_env("sendkey_env")
        if not sendkey:
            return False

        url = f"https://sct.ftqq.com/{sendkey}.send"

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(url, data=payload)
                response.raise_for_status()
                return True
        except Exception:  # noqa: BLE001
            return False

    def _format_desp(self, item: NewsItem) -> str:
        lines = [f"# {item.title}\n"]
        if item.classification:
            lines.append(
                f"**\u91cd\u8981\u6027:** {item.classification.importance_score:.0%}"
            )
            if item.classification.related_tickers:
                lines.append(
                    f"**\u6807\u7684:** {', '.join(item.classification.related_tickers)}"
                )
        if item.analysis:
            lines.append(f"\n## {item.analysis.headline}\n")
            lines.append(item.analysis.what_happened)
            if item.analysis.actionable:
                lines.append(f"\n**\U0001f4a1 \u5efa\u8bae:** {item.analysis.actionable}")
        lines.append(f"\n[\u67e5\u770b\u539f\u6587]({item.url})")
        return "\n".join(lines)
