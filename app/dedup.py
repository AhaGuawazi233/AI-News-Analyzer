"""Multi-layer deduplication for the news-analyzer pipeline.

Layers (collect stage):
  1. URL hash exact match (Redis String, TTL 48h)
  2. SimHash Hamming distance ≤ 3 for long text > 200 chars (Redis Sorted Set)
  3. Title Jaccard similarity ≥ 0.8 for short text ≤ 200 chars (Redis Set)

Second pass (fetch stage / ADR-011):
  SimHash on full article content against the same 48h sorted set.

All Redis operations swallow connection errors and return "not duplicate"
on failure — a false negative is cheaper than a pipeline stall.
"""

from __future__ import annotations

import logging
import re
import time

import redis
from simhash import Simhash

from app.schemas import NewsItem

logger = logging.getLogger(__name__)


class Deduplicator:
    """Multi-layer deduplication: URL hash → SimHash (long text) → Title Jaccard (short text).
    Plus second-pass SimHash for fetched content (v3 / ADR-011)."""

    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        use_simhash: bool = True,
        hamming_threshold: int = 3,
        short_text_threshold: int = 200,
        title_jaccard_threshold: float = 0.8,
        ttl_hours: int = 48,
    ) -> None:
        self.redis = redis_client
        self.use_simhash = use_simhash
        self.hamming_threshold = hamming_threshold
        self.short_text_threshold = short_text_threshold
        self.title_jaccard_threshold = title_jaccard_threshold
        self.ttl_seconds = ttl_hours * 3600

    # ------------------------------------------------------------------
    # Public API — collect stage
    # ------------------------------------------------------------------

    def is_duplicate(self, item: NewsItem) -> bool:
        """First-pass deduplication (collect stage): URL hash + layered fingerprint.
        Returns True if duplicate detected.
        """
        # Layer 1: URL hash exact match
        url_key = f"dedup:url:{item.url_hash}"
        try:
            if self.redis.exists(url_key):
                return True
        except redis.RedisError:
            logger.warning("Redis error checking URL hash for %s", item.url_hash)
            return False

        # Layer 2/3: Content fingerprint
        text = item.content or item.summary or item.title
        if not text:
            return False

        if len(text) > self.short_text_threshold:
            # Long text: SimHash
            if self.use_simhash:
                simhash = Simhash(text)
                return self._check_simhash_duplicate(simhash)
        else:
            # Short text: Title normalization + Jaccard
            return self._check_title_duplicate(item.title)

        return False

    def remember(self, item: NewsItem) -> None:
        """Store fingerprints with TTL 48h."""
        # Store URL hash
        url_key = f"dedup:url:{item.url_hash}"
        try:
            self.redis.setex(url_key, self.ttl_seconds, "1")
        except redis.RedisError:
            logger.warning("Redis error storing URL hash for %s", item.url_hash)
            return

        # Store content fingerprint
        text = item.content or item.summary or item.title
        if text:
            if len(text) > self.short_text_threshold and self.use_simhash:
                simhash = Simhash(text)
                self._store_simhash(simhash)
            else:
                self._store_title_fingerprint(item.title)

    # ------------------------------------------------------------------
    # Public API — fetch stage (v3 / ADR-011)
    # ------------------------------------------------------------------

    def check_duplicate_by_content(self, content: str) -> bool:
        """Second-pass SimHash intercept (fetch stage / ADR-011).
        Check full article content against 48h SimHash index.
        Returns True if duplicate → fetch_task sets status=duplicate_dropped.
        """
        if not content or len(content) < self.short_text_threshold:
            return False

        if not self.use_simhash:
            return False

        simhash = Simhash(content)
        return self._check_simhash_duplicate(simhash)

    def remember_content(self, content: str) -> None:
        """Store content SimHash after second-pass check passes."""
        if content and len(content) >= self.short_text_threshold and self.use_simhash:
            simhash = Simhash(content)
            self._store_simhash(simhash)

    # ------------------------------------------------------------------
    # Internal — SimHash (Redis Sorted Set)
    # ------------------------------------------------------------------

    def _check_simhash_duplicate(self, simhash: Simhash) -> bool:
        """Check SimHash against Redis sorted set with Hamming distance."""
        key = "dedup:simhash"
        try:
            stored = self.redis.zrangebyscore(key, "-inf", "+inf")
        except redis.RedisError:
            logger.warning("Redis error reading SimHash index")
            return False

        for stored_hash_str in stored:
            stored_hash = int(stored_hash_str)
            distance = bin(stored_hash ^ simhash.value).count("1")
            if distance <= self.hamming_threshold:
                return True

        return False

    def _store_simhash(self, simhash: Simhash) -> None:
        """Store SimHash in Redis sorted set with timestamp score."""
        key = "dedup:simhash"
        score = time.time()
        try:
            self.redis.zadd(key, {str(simhash.value): score})
            # Trim entries older than TTL
            cutoff = score - self.ttl_seconds
            self.redis.zremrangebyscore(key, "-inf", cutoff)
        except redis.RedisError:
            logger.warning("Redis error storing SimHash %s", simhash.value)

    # ------------------------------------------------------------------
    # Internal — Title Jaccard (Redis Set, bucketed by prefix)
    # ------------------------------------------------------------------

    def _check_title_duplicate(self, title: str) -> bool:
        """Check title similarity using Jaccard index on normalized words."""
        normalized = self._normalize_title(title)
        if not normalized:
            return False

        prefix = normalized[:8] if len(normalized) >= 8 else normalized
        key = f"dedup:title:{prefix}"

        try:
            stored_titles = self.redis.smembers(key)
        except redis.RedisError:
            logger.warning("Redis error reading title bucket %s", prefix)
            return False

        for stored in stored_titles:
            jaccard = self._jaccard_similarity(normalized, stored)
            if jaccard >= self.title_jaccard_threshold:
                return True

        return False

    def _store_title_fingerprint(self, title: str) -> None:
        """Store normalized title in Redis set."""
        normalized = self._normalize_title(title)
        if not normalized:
            return

        prefix = normalized[:8] if len(normalized) >= 8 else normalized
        key = f"dedup:title:{prefix}"

        try:
            self.redis.sadd(key, normalized)
            self.redis.expire(key, self.ttl_seconds)
        except redis.RedisError:
            logger.warning("Redis error storing title fingerprint for %s", prefix)

    # ------------------------------------------------------------------
    # Pure helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize title: lowercase, remove punctuation, collapse spaces."""
        if not title:
            return ""
        normalized = title.lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @staticmethod
    def _jaccard_similarity(s1: str, s2: str) -> float:
        """Calculate Jaccard similarity between two strings (word-level)."""
        set1 = set(s1.split())
        set2 = set(s2.split())
        if not set1 or not set2:
            return 0.0
        intersection = set1 & set2
        union = set1 | set2
        return len(intersection) / len(union)
