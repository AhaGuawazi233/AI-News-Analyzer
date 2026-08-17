"""Deep analyzer with degraded brief alert fallback.

Uses large model (gpt-4o) for deep analysis. Falls back to brief alert
(no LLM call) when content is too short to avoid hallucination.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jinja2 import Template

from app.schemas import Analysis, ImpactAssessment, NewsItem

if TYPE_CHECKING:
    from app.llm_client import LLMClient


class Analyzer:
    """Deep analysis using large model (gpt-4o).

    Falls back to brief alert (no LLM call) when content is too short
    to avoid hallucination.
    """

    def __init__(
        self,
        client: LLMClient,
        prompts: dict[str, str],
        watchlist_context: str,
        min_content_length: int = 200,
    ) -> None:
        self.client = client
        self.prompts = prompts
        self.watchlist_context = watchlist_context
        self.min_content_length = min_content_length

    def analyze(self, item: NewsItem) -> Analysis:
        """Perform deep analysis on important news.

        If content is too short (< min_content_length), falls back to
        make_brief_alert() to avoid LLM hallucination.
        """
        content = item.content or item.summary or ""

        # Check content sufficiency
        if len(content) < self.min_content_length:
            return self.make_brief_alert(item)

        # Render prompts
        system_template = Template(self.prompts["analyzer_system"])
        user_template = Template(self.prompts["analyzer_user"])

        # Build classification context
        classification_ctx = ""
        if item.classification:
            classification_ctx = (
                f"Category: {item.classification.category}\n"
                f"Related tickers: {', '.join(item.classification.related_tickers)}\n"
                f"Affected markets: {', '.join(item.classification.affected_markets)}\n"
                f"Importance: {item.classification.importance_score}"
            )

        system_prompt = system_template.render(
            watchlist_context=self.watchlist_context,
        )
        user_prompt = user_template.render(
            title=item.title,
            content=content,
            classification=classification_ctx,
            watchlist_context=self.watchlist_context,
        )

        # Call LLM
        result = self.client.chat_json(system_prompt, user_prompt)

        # Parse impact assessments
        impacts: list[ImpactAssessment] = []
        for ia in result.get("impact_assessments", []):
            impacts.append(
                ImpactAssessment(
                    ticker=ia.get("ticker", ""),
                    direction=ia.get("direction", "neutral"),
                    magnitude=ia.get("magnitude", "low"),
                    reasoning=ia.get("reasoning", ""),
                )
            )

        analysis = Analysis(
            headline=result.get("headline", item.title),
            what_happened=result.get("what_happened", ""),
            why_it_matters=result.get("why_it_matters", ""),
            impact_assessments=impacts,
            key_risks=result.get("key_risks", []),
            confidence=result.get("confidence", 0.5),
            actionable=result.get("actionable", ""),
            degraded=False,
            model=self.client.model,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

        return analysis

    def make_brief_alert(self, item: NewsItem) -> Analysis:
        """Generate degraded brief alert without LLM call.

        Used when content is too short for reliable deep analysis.
        Returns low-confidence alert based on classification data only.
        """
        # Extract what we can from classification
        headline = item.title
        category = "general"
        tickers: list[str] = []

        if item.classification:
            category = item.classification.category
            tickers = item.classification.related_tickers

        what_happened = f"[速报] {item.title}"
        if item.summary:
            what_happened += f"\n\n摘要: {item.summary}"

        why_it_matters = f"该新闻被分类为 {category} 类别"
        if tickers:
            why_it_matters += f"，涉及标的: {', '.join(tickers)}"
        why_it_matters += "。由于正文内容不足，无法进行深度分析，请参考原文。"

        impacts: list[ImpactAssessment] = []
        for ticker in tickers[:3]:  # Max 3 impacts for brief
            impacts.append(
                ImpactAssessment(
                    ticker=ticker,
                    direction="uncertain",
                    magnitude="uncertain",
                    reasoning="内容不足，需人工判断",
                )
            )

        return Analysis(
            headline=f"[速报] {headline}",
            what_happened=what_happened,
            why_it_matters=why_it_matters,
            impact_assessments=impacts,
            key_risks=["内容不足，分析置信度低"],
            confidence=0.3,
            actionable=f"建议阅读原文: {item.url}",
            degraded=True,
            model="brief_alert",
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )
