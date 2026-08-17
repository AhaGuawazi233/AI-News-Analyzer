"""Pydantic v2 data contracts for the news-analyzer pipeline.

These schemas define the shared vocabulary between pipeline stages:
collector → deduplicator → fetcher → classifier → analyzer → notifier.

Every boundary crossing (Redis queue, API request/response, DB ↔ domain)
converts raw dicts into these typed models via ``model_validate``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ImpactAssessment(BaseModel):
    """Single ticker impact entry produced by the analyzer."""

    ticker: str
    direction: str  # "up" | "down" | "neutral"
    magnitude: str  # "high" | "medium" | "low"
    reasoning: str


class Classification(BaseModel):
    """Classifier output stored in the ``classification`` JSONB column."""

    is_financial: bool
    is_important: bool
    importance_score: float = Field(ge=0, le=1)
    is_major: bool = False  # paywall initial filter
    is_semantic_duplicate: bool = False  # semantic dedup flag (v3)
    category: str
    related_tickers: list[str] = Field(default_factory=list)
    affected_markets: list[str] = Field(default_factory=list)
    reason: str = ""  # includes "semantic duplicate" marker
    model: str = ""
    classified_at: str


class Analysis(BaseModel):
    """Analyzer output stored in the ``analysis`` JSONB column."""

    headline: str
    what_happened: str
    why_it_matters: str
    impact_assessments: list[ImpactAssessment] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.0)
    actionable: str = ""
    degraded: bool = False  # True when fallback brief alert was used
    model: str = ""
    analyzed_at: str


class NewsItem(BaseModel):
    """Domain representation of a news item — flows through the entire pipeline.

    Mirrors the PostgreSQL ``news_items`` table but uses ``Optional[str]``
    for timestamps so the model stays serialization-friendly.
    """

    id: int | None = None
    title: str
    url: str
    source: str
    source_type: str  # rss / rsshub / google_news
    published: str | None = None
    summary: str | None = None
    content: str | None = None
    content_source: str = "rss_summary"  # rss_summary|full_text|archive_ph|google_news_alt
    lang: str = "en"
    collected_at: str
    url_hash: str
    is_paywalled: bool = False
    content_fingerprint: str = ""
    classification: Classification | None = None
    analysis: Analysis | None = None
    alert_type: str = "analysis"  # analysis|brief
    status: str = "collected"  # collected|fetched|duplicate_dropped|classified|analyzed


class SourceConfig(BaseModel):
    """Configuration for a single news source loaded from YAML."""

    name: str
    type: str  # rss / rsshub / google_news
    lang: str = "en"
    priority: str = "medium"
    proxy: str | None = None
    url: str = ""
    rsshub_base: str = ""
    route: str = ""
    query: str = ""
    is_paywalled: bool = False
