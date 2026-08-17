"""Notification module — Strategy pattern with registry and dispatcher.

Usage::

    from app.notifier import NOTIFIER_REGISTRY, NotifierDispatcher

    channels = [
        NOTIFIER_REGISTRY["feishu"](config),
        NOTIFIER_REGISTRY["telegram"](config),
    ]
    dispatcher = NotifierDispatcher(channels)
    results = dispatcher.dispatch(news_item)
"""

from __future__ import annotations

from app.notifier._stub import IMessageNotifier, WhatsAppNotifier
from app.notifier.bark import BarkNotifier
from app.notifier.base import BaseNotifier, NotifierDispatcher
from app.notifier.dingtalk import DingTalkNotifier
from app.notifier.discord import DiscordNotifier
from app.notifier.feishu import FeishuNotifier
from app.notifier.serverchan import ServerChanNotifier
from app.notifier.slack import SlackNotifier
from app.notifier.telegram import TelegramNotifier
from app.notifier.wecom import WeComNotifier

NOTIFIER_REGISTRY: dict[str, type[BaseNotifier]] = {
    "feishu": FeishuNotifier,
    "telegram": TelegramNotifier,
    "discord": DiscordNotifier,
    "wecom": WeComNotifier,
    "dingtalk": DingTalkNotifier,
    "slack": SlackNotifier,
    "bark": BarkNotifier,
    "serverchan": ServerChanNotifier,
    # v0.2 reserved:
    # "imessage": IMessageNotifier,
    # "whatsapp": WhatsAppNotifier,
}

__all__ = ["NotifierDispatcher", "NOTIFIER_REGISTRY"]
