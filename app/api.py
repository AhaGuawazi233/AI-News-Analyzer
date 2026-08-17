"""FastAPI application for news-analyzer with Obsidian report endpoints."""

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Float, text
from sqlalchemy.orm import Session

from app.config import config
from app.db import get_db_session, init_db
from app.models import NewsItemORM


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables on startup."""
    init_db()
    yield


app = FastAPI(
    title="News Analyzer API",
    description="AI-powered news analysis system for A-shares and US stocks",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    redis: bool
    postgres: bool


class NewsListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Require a configured bearer token for every non-health endpoint."""
    expected_token = os.getenv("API_AUTH_TOKEN")
    if not expected_token:
        raise HTTPException(503, "API authentication is not configured")

    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(supplied_token, expected_token)
    ):
        raise HTTPException(
            401,
            "Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint for monitoring."""
    redis_ok = False
    postgres_ok = False

    # Check Redis
    try:
        config.redis_client.ping()
        redis_ok = True
    except Exception:
        pass

    # Check PostgreSQL
    try:
        with get_db_session() as session:
            session.execute(text("SELECT 1"))
            postgres_ok = True
    except Exception:
        pass

    return HealthResponse(
        status="healthy" if (redis_ok and postgres_ok) else "degraded",
        timestamp=datetime.now(timezone.utc).isoformat(),
        redis=redis_ok,
        postgres=postgres_ok,
    )


@app.get("/api/news", response_model=NewsListResponse)
async def list_news(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    since: str | None = Query(None, description="ISO datetime filter"),
    _auth: None = Depends(require_api_token),
) -> NewsListResponse:
    """List news items with optional filters."""
    with get_db_session() as session:
        query = session.query(NewsItemORM)

        if status:
            query = query.filter(NewsItemORM.status == status)

        if since:
            try:
                since_dt = datetime.fromisoformat(since)
                query = query.filter(NewsItemORM.collected_at >= since_dt)
            except ValueError:
                raise HTTPException(400, "Invalid since datetime format")

        total = query.count()
        items = (
            query.order_by(NewsItemORM.collected_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return NewsListResponse(
            items=[_item_to_dict(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@app.get("/api/news/{item_id}")
async def get_news_item(
    item_id: int,
    _auth: None = Depends(require_api_token),
) -> dict[str, Any]:
    """Get single news item by ID."""
    with get_db_session() as session:
        item = session.get(NewsItemORM, item_id)
        if not item:
            raise HTTPException(404, "News item not found")
        return _item_to_dict(item)


@app.get("/api/report")
async def generate_report(
    hours: int = Query(24, ge=1, le=168, description="Hours to look back"),
    _auth: None = Depends(require_api_token),
) -> dict[str, Any]:
    """Generate Obsidian-format Markdown report of analyzed news."""
    with get_db_session() as session:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        items = (
            session.query(NewsItemORM)
            .filter(
                NewsItemORM.status == "analyzed",
                NewsItemORM.collected_at >= since,
            )
            .order_by(
                NewsItemORM.classification["importance_score"]
                .astext.cast(Float)
                .desc(),
                NewsItemORM.collected_at.desc(),
            )
            .limit(100)
            .all()
        )

        if not items:
            return {"content": "No analyzed news in the specified period.", "count": 0}

        reports = [_generate_obsidian_note(item) for item in items]
        combined = "\n\n---\n\n".join(reports)
        return {"content": combined, "count": len(reports)}


@app.post("/api/trigger/collect")
async def trigger_collection(
    _auth: None = Depends(require_api_token),
) -> dict[str, Any]:
    """Manually trigger news collection."""
    from app.tasks import collect_task

    result = collect_task.delay()
    return {"status": "triggered", "task_id": result.id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item_to_dict(item: NewsItemORM) -> dict[str, Any]:
    """Convert ORM model to API response dict."""
    content = item.content
    if content and len(content) > 500:
        content = content[:500] + "..."

    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "source_type": item.source_type,
        "is_paywalled": item.is_paywalled,
        "published": item.published.isoformat() if item.published else None,
        "summary": item.summary,
        "content": content,
        "content_source": item.content_source,
        "lang": item.lang,
        "collected_at": item.collected_at.isoformat() if item.collected_at else None,
        "classification": item.classification,
        "analysis": item.analysis,
        "alert_type": item.alert_type,
        "status": item.status,
    }


def _generate_obsidian_note(item: NewsItemORM) -> str:
    """Generate Obsidian note with YAML frontmatter."""
    classification = item.classification or {}
    analysis = item.analysis or {}

    tickers: list[str] = classification.get("related_tickers", [])
    importance: float = classification.get("importance_score", 0)
    category: str = classification.get("category", "general")

    # Build tags
    tags = ["news-analysis"]
    if category:
        tags.append(category)
    tags.extend(t.lower().replace(" ", "-") for t in tickers[:3])

    collected_iso = (
        item.collected_at.isoformat()
        if item.collected_at
        else datetime.now(timezone.utc).isoformat()
    )

    frontmatter = f"""---
created: {collected_iso}
source: {item.source}
importance: {importance}
confidence: {analysis.get('confidence', 0)}
tickers: {tickers}
tags: {tags}
degraded: {analysis.get('degraded', False)}
status: {item.status}
---"""

    headline: str = analysis.get("headline", item.title)
    what_happened: str = analysis.get("what_happened", item.summary or "")
    why_it_matters: str = analysis.get("why_it_matters", "")
    actionable: str = analysis.get("actionable", "")

    # Impact assessments
    impacts: list[dict[str, Any]] = analysis.get("impact_assessments", [])
    impact_text = ""
    if impacts:
        impact_lines = [
            f"- **{ia.get('ticker', '')}**: {ia.get('direction', '')} "
            f"({ia.get('magnitude', '')}) - {ia.get('reasoning', '')}"
            for ia in impacts
        ]
        impact_text = "\n\n## 影响评估\n" + "\n".join(impact_lines)

    # Key risks
    risks: list[str] = analysis.get("key_risks", [])
    risk_text = ""
    if risks:
        risk_text = "\n\n## 关键风险\n" + "\n".join(f"- {r}" for r in risks)

    body = f"""# {headline}

## 事件
{what_happened}

## 意义
{why_it_matters}
{impact_text}
{risk_text}

## 建议
{actionable}

[原文链接]({item.url})
"""

    return f"{frontmatter}\n\n{body}"
