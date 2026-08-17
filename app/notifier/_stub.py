"""Reserved notifier stubs for v0.2.

These are placeholder implementations for channels planned in a future release.
"""

from __future__ import annotations

from app.notifier.base import BaseNotifier


class IMessageNotifier(BaseNotifier):
    """v0.2: iMessage via Pushover/Bark bridge or macOS AppleScript."""

    channel_type = "imessage"

    def _build_payload(self, item: object) -> dict:
        raise NotImplementedError("iMessage notifier not implemented in v0.1")

    def _post(self, payload: dict) -> bool:
        return False


class WhatsAppNotifier(BaseNotifier):
    """v0.2: WhatsApp via Twilio or official Cloud API."""

    channel_type = "whatsapp"

    def _build_payload(self, item: object) -> dict:
        raise NotImplementedError("WhatsApp notifier not implemented in v0.1")

    def _post(self, payload: dict) -> bool:
        return False
