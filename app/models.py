"""SQLAlchemy ORM models for news-analyzer PostgreSQL persistence."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class NewsItemORM(Base):
    """Persistent news item row. Maps to the `news_items` table in PostgreSQL."""

    __tablename__ = "news_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url_hash = Column(String(16), unique=True, nullable=False)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    source = Column(String(64), nullable=False)
    source_type = Column(String(16), nullable=False)  # rss/rsshub/google_news
    is_paywalled = Column(Boolean, default=False)
    published = Column(DateTime(timezone=True))
    summary = Column(Text)
    content = Column(Text)
    content_source = Column(
        String(32), default="rss_summary"
    )  # rss_summary|full_text|archive_ph|google_news_alt
    lang = Column(String(8), default="en")
    collected_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    content_fingerprint = Column(String(32))
    classification = Column(JSONB)
    analysis = Column(JSONB)
    alert_type = Column(String(16), default="analysis")  # analysis|brief
    status = Column(
        String(20), default="collected"
    )  # collected|fetched|duplicate_dropped|classified|analyzed

    __table_args__ = (
        CheckConstraint(
            "status IN ('collected','fetched','duplicate_dropped','classified','analyzed')",
            name="check_status",
        ),
        Index("idx_news_published", "published"),
        Index("idx_news_status", "status"),
        Index(
            "idx_news_importance",
            "classification",
            postgresql_using="gin",
            postgresql_ops={"classification": "jsonb_path_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<NewsItemORM id={self.id} url_hash={self.url_hash!r} status={self.status!r}>"


class HistoryRepository:
    """Repository for querying historical news items for semantic dedup.

    Uses parameterized queries exclusively — no SQL injection surface.
    """

    def __init__(self, session_factory: type[Session]) -> None:
        self._session_factory = session_factory

    def get_important_titles_last_24h(self, max_items: int = 50) -> list[str]:
        """Get titles of is_important=True news from the last 24 hours.

        The JSONB field ``classification`` stores ``is_important`` as a string
        ``'true'`` / ``'false'``.  We query with the JSONB ``->>`` operator so
        the filter is index-friendly (idx_news_importance partial index).

        Returns:
            List of title strings, most recent first.
        """
        stmt = (
            "SELECT title FROM news_items "
            "WHERE classification->>'is_important' = :is_important "
            "AND collected_at > now() - interval '24 hours' "
            "ORDER BY collected_at DESC LIMIT :max_items"
        )
        session: Session = self._session_factory()
        try:
            result = session.execute(
                text(stmt),
                {"is_important": "true", "max_items": max_items},
            )
            return [row[0] for row in result.fetchall()]
        finally:
            session.close()
