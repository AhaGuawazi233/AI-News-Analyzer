"""Celery tasks for the news-analyzer pipeline.  # allow: SIZE_OK — pipeline state machine; tasks dispatch each other (fetch→classify→analyze→notify), splitting causes circular imports.

Flow: collect → fetch → classify → analyze → notify
Each task receives item_id (ADR-007) and checks idempotency at entry (ADR-013).
Status updates use optimistic locking (UPDATE ... WHERE status=?).
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError

from app.celery_app import celery_app
from app.classifier import Classifier
from app.collectors.google_news import GoogleNewsCollector
from app.collectors.rss import RSSCollector
from app.collectors.rsshub import RSSHubCollector
from app.config import config
from app.db import get_db_session
from app.dedup import Deduplicator
from app.fetcher import ContentFetcher
from app.llm_client import LLMClient
from app.models import HistoryRepository, NewsItemORM
from app.rate_limiter import RateLimiter, RateLimitTimeoutError
from app.schemas import NewsItem, SourceConfig

logger = logging.getLogger(__name__)

# Status order for idempotency checks (ADR-013)
STATUS_ORDER = ["collected", "fetched", "duplicate_dropped", "classified", "analyzed"]


def _check_idempotent(item_id: int, target_status: str, session) -> bool:
    """v3: Idempotency guard. Returns True if should skip (already at or past target status).

    Prevents duplicate LLM calls and notifications on task replay.
    """
    item = session.get(NewsItemORM, item_id)
    if item is None:
        return True  # Item doesn't exist, skip

    current_idx = STATUS_ORDER.index(item.status) if item.status in STATUS_ORDER else -1
    target_idx = STATUS_ORDER.index(target_status)

    if current_idx >= target_idx:
        return True  # Already at or past target status

    return False


def _build_news_item(orm: NewsItemORM) -> NewsItem:
    """Convert ORM model to Pydantic schema."""
    return NewsItem(
        id=orm.id,
        title=orm.title,
        url=orm.url,
        source=orm.source,
        source_type=orm.source_type,
        published=orm.published.isoformat() if orm.published else None,
        summary=orm.summary,
        content=orm.content,
        content_source=orm.content_source,
        lang=orm.lang,
        collected_at=orm.collected_at.isoformat(),
        url_hash=orm.url_hash,
        content_fingerprint=orm.content_fingerprint or "",
        classification=orm.classification,
        analysis=orm.analysis,
        alert_type=orm.alert_type,
        status=orm.status,
    )


@celery_app.task(name="collect", queue="collect")
def collect_task():
    """Beat-triggered collection from all configured RSS sources.

    Flow: iterate sources → collect items → dedup → insert to DB → dispatch fetch tasks
    """
    logger.info("Starting collection task")

    # Build deduplicator
    dedup = Deduplicator(
        redis_client=config.redis_client,
        use_simhash=config.settings["dedup"]["use_simhash"],
        hamming_threshold=config.settings["dedup"]["hamming_threshold"],
        short_text_threshold=config.settings["dedup"]["short_text_threshold"],
        title_jaccard_threshold=config.settings["dedup"]["title_jaccard_threshold"],
        ttl_hours=config.settings["dedup"]["ttl_hours"],
    )

    # Get default proxy
    default_proxy = config.settings["proxy"].get("default") or None

    total_collected = 0
    total_deduped = 0

    for source_cfg in config.rss_sources.get("sources", []):
        try:
            source = SourceConfig(**source_cfg)

            # Select collector based on type
            if source.type == "rss":
                collector = RSSCollector(source, default_proxy=default_proxy)
            elif source.type == "rsshub":
                collector = RSSHubCollector(source, default_proxy=default_proxy)
            elif source.type == "google_news":
                collector = GoogleNewsCollector(source, default_proxy=default_proxy)
            else:
                logger.warning(f"Unknown source type: {source.type}")
                continue

            # Collect items
            items = collector.fetch()
            logger.info(f"Collected {len(items)} items from {source.name}")

            for item in items:
                # Check deduplication
                if dedup.is_duplicate(item):
                    total_deduped += 1
                    continue

                # Insert to database. Dispatch only after the transaction commits so
                # a fast worker can always see the row it was asked to process.
                try:
                    item_id: int | None = None
                    is_new_item = False
                    with get_db_session() as session:
                        # Check if URL already exists (unique constraint)
                        existing = session.query(NewsItemORM).filter_by(
                            url_hash=item.url_hash
                        ).first()

                        if existing:
                            # A previous broker publish may have failed after the DB
                            # commit. Re-dispatch only rows that never advanced.
                            if existing.status == "collected":
                                item_id = existing.id
                            else:
                                total_deduped += 1
                                continue
                        else:
                            orm_item = NewsItemORM(
                                url_hash=item.url_hash,
                                title=item.title,
                                url=item.url,
                                source=item.source,
                                source_type=item.source_type,
                                is_paywalled=getattr(item, "is_paywalled", False),
                                published=item.published,
                                summary=item.summary,
                                content=item.content,
                                content_source=item.content_source,
                                lang=item.lang,
                                collected_at=item.collected_at,
                                status="collected",
                            )
                            session.add(orm_item)
                            session.flush()  # Get the ID before commit
                            item_id = orm_item.id
                            is_new_item = True

                    # get_db_session commits before control reaches this point.
                    if item_id is None:
                        continue
                    fetch_task.delay(item_id)
                    # Only suppress future collection after broker publication
                    # succeeds. If it fails, the next collection can re-dispatch.
                    dedup.remember(item)
                    if is_new_item:
                        total_collected += 1

                except IntegrityError:
                    # Race condition: another task inserted the same url_hash
                    logger.debug(f"Duplicate url_hash {item.url_hash}, skipping")
                    total_deduped += 1
                    continue

        except Exception as e:
            logger.error(f"Error collecting from {source_cfg.get('name', 'unknown')}: {e}")
            continue

    logger.info(f"Collection complete: {total_collected} collected, {total_deduped} deduped")
    return {"collected": total_collected, "deduped": total_deduped}


@celery_app.task(name="fetch", queue="fetch", bind=True, max_retries=2)
def fetch_task(self, item_id: int):
    """Fetch full article text with second-pass SimHash dedup (v3 / ADR-011).

    Flow: idempotency check → fetch content → second SimHash → optimistic status update → dispatch classify
    """
    logger.info(f"Fetch task for item {item_id}")

    with get_db_session() as session:
        # v3: Idempotency guard (ADR-013)
        if _check_idempotent(item_id, "fetched", session):
            logger.info(f"Item {item_id} already fetched or beyond, skipping")
            return

        item = session.get(NewsItemORM, item_id)
        if not item:
            logger.warning(f"Item {item_id} not found")
            return

        # Build fetcher
        default_proxy = config.settings["proxy"].get("default") or None
        fetcher = ContentFetcher(
            proxy=default_proxy,
            archive_ph_timeout=config.settings["fetcher"]["archive_ph_timeout"],
            source_timeout=config.settings["fetcher"]["source_timeout"],
            cloudflare_markers=config.settings["fetcher"]["cloudflare_markers"],
        )

        # Fetch full text
        news_item = _build_news_item(item)
        content, content_source = fetcher.fetch_full_text(news_item)

        # v3: Second-pass SimHash if we got content (ADR-011)
        if content and config.settings["dedup"]["second_pass_simhash"]:
            dedup = Deduplicator(
                redis_client=config.redis_client,
                use_simhash=config.settings["dedup"]["use_simhash"],
                hamming_threshold=config.settings["dedup"]["hamming_threshold"],
                ttl_hours=config.settings["dedup"]["ttl_hours"],
            )

            if dedup.check_duplicate_by_content(content):
                # Duplicate content detected → terminate pipeline
                # Optimistic locking: only update if still at 'collected'
                updated = session.query(NewsItemORM).filter(
                    NewsItemORM.id == item_id,
                    NewsItemORM.status == "collected",
                ).update({"status": "duplicate_dropped"})
                if updated == 0:
                    logger.info(f"Item {item_id} status already changed during dedup")
                    return
                session.commit()
                logger.info(f"Item {item_id} dropped by second-pass SimHash")
                return

            # Remember content fingerprint
            dedup.remember_content(content)

        # Optimistic locking: only update if still at 'collected'
        update_fields: dict = {
            "status": "fetched",
        }
        if content:
            update_fields["content"] = content
            update_fields["content_source"] = content_source

        updated = session.query(NewsItemORM).filter(
            NewsItemORM.id == item_id,
            NewsItemORM.status == "collected",
        ).update(update_fields)

        if updated == 0:
            logger.info(f"Item {item_id} status already changed, skipping")
            return

        session.commit()

        # Dispatch classify task
        classify_task.delay(item_id)
        logger.info(f"Item {item_id} fetched, dispatched to classify")


@celery_app.task(name="classify", queue="classify", bind=True, max_retries=2)
def classify_task(self, item_id: int):
    """Classify news item using small model with semantic dedup (v3 / ADR-012).

    Flow: idempotency check → classify → optimistic status update → dispatch analyze if important
    """
    logger.info(f"Classify task for item {item_id}")

    with get_db_session() as session:
        # v3: Idempotency guard (ADR-013)
        if _check_idempotent(item_id, "classified", session):
            logger.info(f"Item {item_id} already classified or beyond, skipping")
            return

        item = session.get(NewsItemORM, item_id)
        if not item:
            logger.warning(f"Item {item_id} not found")
            return

        # Build classifier
        rate_limiter = RateLimiter(
            redis_client=config.redis_client,
            key="small_model",
            rpm=config.settings["rate_limit"]["small_model_rpm"],
        )
        llm_client = LLMClient(
            provider=config.small_model_config.provider,
            model=config.small_model_config.model,
            api_key=config.small_model_config.api_key,
            base_url=config.small_model_config.base_url,
            temperature=config.small_model_config.temperature,
            max_tokens=config.small_model_config.max_tokens,
            rate_limiter=rate_limiter,
        )

        # Build watchlist keywords
        watchlist_keywords = []
        for stock in config.watchlist.get("a_shares", []) + config.watchlist.get("us_stocks", []):
            watchlist_keywords.append(stock["name"])
            watchlist_keywords.extend(stock.get("aliases", []))
        watchlist_keywords.extend(config.watchlist.get("macro_themes", []))

        # Build history repository for semantic dedup (ADR-012)
        history_repo = HistoryRepository(config.SessionLocal)

        classifier = Classifier(
            client=llm_client,
            prompts=config.prompts,
            watchlist_keywords=watchlist_keywords,
            importance_threshold=config.settings["importance_threshold"],
            history_repo=history_repo,
            semantic_dedup_enabled=config.settings["semantic_dedup"]["enabled"],
            max_history_items=config.settings["semantic_dedup"]["max_history_items"],
        )

        # Classify
        news_item = _build_news_item(item)
        classification = classifier.classify(news_item)

        # Optimistic locking: only update if still at 'fetched'
        updated = session.query(NewsItemORM).filter(
            NewsItemORM.id == item_id,
            NewsItemORM.status == "fetched",
        ).update({
            "status": "classified",
            "classification": classification.model_dump(),
        })

        if updated == 0:
            logger.info(f"Item {item_id} status already changed, skipping")
            return

        session.commit()

        # Dispatch analyze task if important
        if classification.is_important and not classification.is_semantic_duplicate:
            analyze_task.delay(item_id)
            logger.info(f"Item {item_id} classified as important, dispatched to analyze")
        else:
            logger.info(f"Item {item_id} classified as not important, pipeline terminated")


@celery_app.task(name="analyze", queue="analyze", bind=True, max_retries=3)
def analyze_task(self, item_id: int):
    """Deep analysis using large model with rate limit retry (v3 / ADR-009).

    Flow: idempotency check → analyze (catches RateLimitTimeoutError) → optimistic status update → dispatch notify

    v3: If LLM rate limited, self.retry(countdown=30) to release worker.
    """
    logger.info(f"Analyze task for item {item_id}")

    with get_db_session() as session:
        # v3: Idempotency guard (ADR-013)
        if _check_idempotent(item_id, "analyzed", session):
            logger.info(f"Item {item_id} already analyzed, skipping")
            return

        item = session.get(NewsItemORM, item_id)
        if not item:
            logger.warning(f"Item {item_id} not found")
            return

        # Build analyzer (lazy import — module created separately)
        from app.analyzer import Analyzer

        rate_limiter = RateLimiter(
            redis_client=config.redis_client,
            key="large_model",
            rpm=config.settings["rate_limit"]["large_model_rpm"],
            tpm=config.settings["rate_limit"]["large_model_tpm"],
        )
        llm_client = LLMClient(
            provider=config.large_model_config.provider,
            model=config.large_model_config.model,
            api_key=config.large_model_config.api_key,
            base_url=config.large_model_config.base_url,
            temperature=config.large_model_config.temperature,
            max_tokens=config.large_model_config.max_tokens,
            rate_limiter=rate_limiter,
        )

        # Build watchlist context string
        watchlist_lines = []
        for stock in config.watchlist.get("a_shares", []) + config.watchlist.get("us_stocks", []):
            watchlist_lines.append(f"- {stock['code']}: {stock['name']} ({stock.get('sector', '')})")
        watchlist_context = "\n".join(watchlist_lines)

        analyzer = Analyzer(
            client=llm_client,
            prompts=config.prompts,
            watchlist_context=watchlist_context,
            min_content_length=config.settings["fetcher"]["min_content_length"],
        )

        # Analyze with rate limit handling (ADR-009)
        try:
            news_item = _build_news_item(item)
            analysis = analyzer.analyze(news_item)
        except RateLimitTimeoutError:
            # v3: Rate limited → retry later, release worker
            logger.warning(f"Item {item_id} rate limited, retrying in 30s")
            raise self.retry(countdown=config.settings["rate_limit"]["retry_countdown"])

        # Optimistic locking: only update if still at 'classified'
        updated = session.query(NewsItemORM).filter(
            NewsItemORM.id == item_id,
            NewsItemORM.status == "classified",
        ).update({
            "status": "analyzed",
            "analysis": analysis.model_dump(),
            "alert_type": "brief" if analysis.degraded else "analysis",
        })

        if updated == 0:
            logger.info(f"Item {item_id} status already changed, skipping")
            return

        session.commit()

        # Dispatch notify task
        notify_task.delay(item_id)
        logger.info(f"Item {item_id} analyzed, dispatched to notify")


@celery_app.task(name="notify", queue="notify")
def notify_task(item_id: int):
    """Send notifications for analyzed news item.

    Reads item from DB → sends to all enabled notification channels.
    """
    logger.info(f"Notify task for item {item_id}")

    with get_db_session() as session:
        item = session.get(NewsItemORM, item_id)
        if not item:
            logger.warning(f"Item {item_id} not found")
            return

        # Check if notifications are enabled
        if not config.settings.get("notifier", {}).get("enabled", False):
            logger.info("Notifications disabled, skipping")
            return

        # Build notifier dispatcher (lazy import — module created separately)
        from app.notifier import NotifierDispatcher, NOTIFIER_REGISTRY

        channels = []
        for ch_cfg in config.settings["notifier"].get("channels", []):
            ch_type = ch_cfg["type"]
            if ch_type in NOTIFIER_REGISTRY:
                notifier_cls = NOTIFIER_REGISTRY[ch_type]
                notifier = notifier_cls(
                    config=ch_cfg,
                    alert_threshold=config.settings["notifier"]["alert_threshold"],
                    brief_alert_threshold=config.settings["notifier"]["brief_alert_threshold"],
                )
                channels.append(notifier)

        if not channels:
            logger.info("No notification channels configured")
            return

        dispatcher = NotifierDispatcher(channels)
        news_item = _build_news_item(item)
        results = dispatcher.dispatch(news_item)

        logger.info(f"Notification results for item {item_id}: {results}")
        return results
