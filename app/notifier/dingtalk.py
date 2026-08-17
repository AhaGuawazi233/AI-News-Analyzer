"""DingTalk webhook notifier with HMAC-SHA256 signature support."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from app.notifier.base import BaseNotifier
from app.schemas import NewsItem


class DingTalkNotifier(BaseNotifier):
    """Send alerts to DingTalk via incoming webhook.

    Supports optional HMAC-SHA256 signature when ``secret_env`` is configured.
    """

    channel_type = "dingtalk"

    def _build_payload(self, item: NewsItem) -> dict:
        text = self._format_message(item)
        return {"msgtype": "text", "text": {"content": text}}

    def _post(self, payload: dict) -> bool:
        webhook_url = self._get_env("webhook_url_env")
        secret = self._get_env("secret_env")

        if not webhook_url:
            return False

        # Add signature if secret configured
        url = webhook_url
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(url, json=payload)
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
