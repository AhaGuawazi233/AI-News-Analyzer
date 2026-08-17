"""News classifier with semantic deduplication (v3).

Uses small model (gpt-4o-mini) to classify news items. Before calling LLM,
injects 24h important news history for semantic dedup. If semantic duplicate
detected, returns is_important=False immediately.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jinja2 import Template

from app.schemas import Classification, NewsItem

if TYPE_CHECKING:
    from app.llm_client import LLMClient
    from app.models import HistoryRepository


class Classifier:
    """News classifier using small model (gpt-4o-mini).

    v3: Includes semantic deduplication - checks if news is duplicate
    of important news from last 24 hours using LLM judgment.
    """

    def __init__(
        self,
        client: LLMClient,
        prompts: dict[str, str],
        watchlist_keywords: list[str],
        importance_threshold: float = 0.6,
        history_repo: HistoryRepository | None = None,
        semantic_dedup_enabled: bool = True,
        max_history_items: int = 50,
    ) -> None:
        self.client = client
        self.prompts = prompts
        self.watchlist_keywords = watchlist_keywords
        self.importance_threshold = importance_threshold
        self.history_repo = history_repo
        self.semantic_dedup_enabled = semantic_dedup_enabled
        self.max_history_items = max_history_items

    def classify(self, item: NewsItem) -> Classification:
        """Classify news item using small model.

        v3: Before calling LLM, injects 24h important news history for semantic dedup.
        If semantic duplicate detected, returns is_important=False immediately.
        """
        # Get history for semantic dedup context
        history_titles: list[str] = []
        if self.semantic_dedup_enabled and self.history_repo:
            history_titles = self.history_repo.get_important_titles_last_24h(
                max_items=self.max_history_items
            )

        # Build history context string
        history_context = ""
        if history_titles:
            history_context = "\n".join(f"- {t}" for t in history_titles)

        # Render prompts with Jinja2
        system_template = Template(self.prompts["classifier_system"])
        user_template = Template(self.prompts["classifier_user"])

        system_prompt = system_template.render(
            watchlist_keywords=", ".join(self.watchlist_keywords),
            history_important_titles=history_context,
        )
        user_prompt = user_template.render(
            title=item.title,
            summary=item.summary or "",
            content=item.content or item.summary or "",
        )

        # Call LLM
        result = self.client.chat_json(system_prompt, user_prompt)

        # Parse response
        classification = Classification(
            is_financial=result.get("is_financial", False),
            is_important=result.get("is_important", False),
            importance_score=result.get("importance_score", 0.0),
            is_major=result.get("is_major", False),
            is_semantic_duplicate=result.get("is_semantic_duplicate", False),
            category=result.get("category", "general"),
            related_tickers=result.get("related_tickers", []),
            affected_markets=result.get("affected_markets", []),
            reason=result.get("reason", ""),
            model=self.client.model,
            classified_at=datetime.now(timezone.utc).isoformat(),
        )

        # Enforce semantic dedup: if flagged, force is_important=False
        if classification.is_semantic_duplicate:
            classification.is_important = False
            if "语义重复" not in classification.reason:
                classification.reason += " [语义重复]"

        # Apply importance threshold
        if classification.importance_score < self.importance_threshold:
            classification.is_important = False

        return classification
