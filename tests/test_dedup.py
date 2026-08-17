"""Tests for app.dedup — multi-layer deduplication.

Given: a Deduplicator wired to a mocked Redis client
When:  various NewsItem / content inputs hit the dedup layers
Then:  correct duplicate / not-duplicate decisions
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import redis

from app.dedup import Deduplicator
from app.schemas import NewsItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_item(**overrides: object) -> NewsItem:
    """Build a NewsItem with sane defaults; caller overrides specific fields."""
    defaults = dict(
        title="Fed raises rates by 25 basis points",
        url="https://reuters.com/fed-raises-rates",
        source="reuters",
        source_type="rss",
        collected_at="2026-08-05T10:00:00Z",
        url_hash="abc123def456",
        summary="The Federal Reserve raised interest rates.",
    )
    defaults.update(overrides)
    return NewsItem(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def mock_redis() -> MagicMock:
    """Fake redis.Redis with all methods returning safe defaults."""
    r = MagicMock(spec=redis.Redis)
    r.exists.return_value = 0
    r.zrangebyscore.return_value = []
    r.smembers.return_value = set()
    return r


@pytest.fixture()
def dedup(mock_redis: MagicMock) -> Deduplicator:
    return Deduplicator(mock_redis)


# ---------------------------------------------------------------------------
# Layer 1 — URL hash exact match
# ---------------------------------------------------------------------------

class TestURLHashLayer:
    def test_is_duplicate_returns_true_when_url_hash_exists(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: URL hash already in Redis  →  When: is_duplicate  →  Then: True"""
        mock_redis.exists.return_value = 1
        item = _make_item()

        assert dedup.is_duplicate(item) is True
        mock_redis.exists.assert_called_once_with("dedup:url:abc123def456")

    def test_is_duplicate_returns_false_when_url_hash_absent(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: URL hash not in Redis, no content  →  When: is_duplicate  →  Then: False"""
        mock_redis.exists.return_value = 0
        item = _make_item(summary=None, content=None, title="")  # no text

        assert dedup.is_duplicate(item) is False

    def test_is_duplicate_returns_false_on_redis_error(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis connection fails  →  When: is_duplicate  →  Then: False (fail-open)"""
        mock_redis.exists.side_effect = redis.ConnectionError("gone")
        item = _make_item()

        assert dedup.is_duplicate(item) is False


# ---------------------------------------------------------------------------
# Layer 2 — SimHash (long text > threshold)
# ---------------------------------------------------------------------------

class TestSimHashLayer:
    def test_long_text_checks_simhash(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: long text, no stored hashes  →  When: is_duplicate  →  Then: False"""
        long_text = "word " * 100  # 500 chars > 200 threshold
        item = _make_item(content=long_text)

        assert dedup.is_duplicate(item) is False
        mock_redis.zrangebyscore.assert_called_once_with("dedup:simhash", "-inf", "+inf")

    def test_long_text_duplicate_when_hamming_le_threshold(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: stored SimHash within Hamming distance  →  Then: True"""
        # Build a SimHash for text, store the same value → distance 0
        from simhash import Simhash

        text = "word " * 100
        sh = Simhash(text)
        mock_redis.zrangebyscore.return_value = [str(sh.value)]

        item = _make_item(content=text)
        assert dedup.is_duplicate(item) is True

    def test_long_text_not_duplicate_when_hamming_gt_threshold(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: stored SimHash with Hamming distance > 3  →  Then: False"""
        from simhash import Simhash

        text = "word " * 100
        sh = Simhash(text)
        # Flip many bits to exceed threshold
        far_hash = sh.value ^ ((1 << 64) - 1)  # all bits flipped → distance 64
        mock_redis.zrangebyscore.return_value = [str(far_hash)]

        item = _make_item(content=text)
        assert dedup.is_duplicate(item) is False

    def test_simhash_disabled_skips_check(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: use_simhash=False  →  When: long text  →  Then: False, no Redis call"""
        dedup = Deduplicator(mock_redis, use_simhash=False)
        item = _make_item(content="word " * 100)

        assert dedup.is_duplicate(item) is False
        mock_redis.zrangebyscore.assert_not_called()

    def test_simhash_redis_error_returns_false(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis fails on zrangebyscore  →  Then: False (fail-open)"""
        mock_redis.zrangebyscore.side_effect = redis.ConnectionError("boom")
        item = _make_item(content="word " * 100)

        assert dedup.is_duplicate(item) is False


# ---------------------------------------------------------------------------
# Layer 3 — Title Jaccard (short text ≤ threshold)
# ---------------------------------------------------------------------------

class TestTitleJaccardLayer:
    def test_short_text_checks_title_jaccard(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: short text (summary < 200 chars), no stored titles  →  Then: False"""
        item = _make_item(summary="Short summary", content=None)

        assert dedup.is_duplicate(item) is False
        mock_redis.smembers.assert_called()

    def test_title_duplicate_when_jaccard_above_threshold(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: stored title nearly identical  →  Then: True"""
        # Normalized: "fed raises rates by 25 basis points"
        stored = "fed raises rates by 25 basis points"
        mock_redis.smembers.return_value = {stored}

        item = _make_item(
            title="Fed Raises Rates by 25 Basis Points!",
            summary=None,
            content=None,
        )
        assert dedup.is_duplicate(item) is True

    def test_title_not_duplicate_when_jaccard_below_threshold(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: stored title with low overlap  →  Then: False"""
        stored = "completely different news about sports"
        mock_redis.smembers.return_value = {stored}

        item = _make_item(
            title="Fed Raises Rates by 25 Basis Points",
            summary=None,
            content=None,
        )
        assert dedup.is_duplicate(item) is False

    def test_empty_title_returns_false(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: empty title + no content/summary  →  Then: False"""
        item = _make_item(title="", summary=None, content=None)
        assert dedup.is_duplicate(item) is False

    def test_title_redis_error_returns_false(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis fails on smembers  →  Then: False (fail-open)"""
        mock_redis.smembers.side_effect = redis.ConnectionError("nope")
        item = _make_item(summary=None, content=None)

        assert dedup.is_duplicate(item) is False


# ---------------------------------------------------------------------------
# remember — store fingerprints
# ---------------------------------------------------------------------------

class TestRemember:
    def test_remember_stores_url_hash(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember  →  Then: setex called with URL key and TTL"""
        item = _make_item()
        dedup.remember(item)

        mock_redis.setex.assert_called_once_with(
            "dedup:url:abc123def456", 48 * 3600, "1"
        )

    def test_remember_stores_simhash_for_long_text(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember with long content  →  Then: zadd called on simhash key"""
        item = _make_item(content="word " * 100)
        dedup.remember(item)

        mock_redis.zadd.assert_called_once()
        args = mock_redis.zadd.call_args
        assert args[0][0] == "dedup:simhash"

    def test_remember_stores_title_for_short_text(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember with short content  →  Then: sadd called on title key"""
        item = _make_item(summary="Short summary", content=None)
        dedup.remember(item)

        mock_redis.sadd.assert_called_once()
        args = mock_redis.sadd.call_args
        assert args[0][0].startswith("dedup:title:")

    def test_remember_skips_fingerprint_when_no_text(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember with no content/summary/title  →  Then: only URL stored"""
        item = _make_item(title="", summary=None, content=None)
        dedup.remember(item)

        mock_redis.setex.assert_called_once()  # URL hash only
        mock_redis.zadd.assert_not_called()
        mock_redis.sadd.assert_not_called()

    def test_remember_handles_redis_error_on_setex(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis setex fails  →  When: remember  →  Then: no exception raised"""
        mock_redis.setex.side_effect = redis.ConnectionError("fail")
        item = _make_item()

        # Should not raise
        dedup.remember(item)

    def test_remember_trims_old_simhash_entries(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember stores SimHash  →  Then: zremrangebyscore trims old entries"""
        item = _make_item(content="word " * 100)
        dedup.remember(item)

        mock_redis.zremrangebyscore.assert_called_once()
        args = mock_redis.zremrangebyscore.call_args[0]
        assert args[0] == "dedup:simhash"
        # cutoff should be roughly (now - ttl)
        cutoff = args[2]
        assert cutoff < time.time()
        assert cutoff > time.time() - 48 * 3600 - 5  # within 5s margin


# ---------------------------------------------------------------------------
# check_duplicate_by_content — second-pass SimHash (fetch stage)
# ---------------------------------------------------------------------------

class TestCheckDuplicateByContent:
    def test_returns_false_for_short_content(self, dedup: Deduplicator) -> None:
        """Given: content shorter than threshold  →  Then: False"""
        assert dedup.check_duplicate_by_content("short") is False

    def test_returns_false_for_empty_content(self, dedup: Deduplicator) -> None:
        """Given: empty content  →  Then: False"""
        assert dedup.check_duplicate_by_content("") is False

    def test_returns_false_for_none_like(self, dedup: Deduplicator) -> None:
        """Given: empty-ish content  →  Then: False"""
        assert dedup.check_duplicate_by_content("") is False

    def test_returns_false_when_no_stored_hashes(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: long content, no stored SimHashes  →  Then: False"""
        content = "word " * 100
        assert dedup.check_duplicate_by_content(content) is False

    def test_returns_true_when_duplicate_found(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: long content with matching SimHash stored  →  Then: True"""
        from simhash import Simhash

        content = "word " * 100
        sh = Simhash(content)
        mock_redis.zrangebyscore.return_value = [str(sh.value)]

        assert dedup.check_duplicate_by_content(content) is True

    def test_returns_false_when_simhash_disabled(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: use_simhash=False  →  Then: False, no Redis call"""
        dedup = Deduplicator(mock_redis, use_simhash=False)
        content = "word " * 100

        assert dedup.check_duplicate_by_content(content) is False
        mock_redis.zrangebyscore.assert_not_called()

    def test_returns_false_on_redis_error(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis fails  →  Then: False (fail-open)"""
        mock_redis.zrangebyscore.side_effect = redis.ConnectionError("down")
        content = "word " * 100

        assert dedup.check_duplicate_by_content(content) is False


# ---------------------------------------------------------------------------
# remember_content — store after second-pass passes
# ---------------------------------------------------------------------------

class TestRememberContent:
    def test_stores_simhash_for_long_content(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember_content with long text  →  Then: zadd called"""
        content = "word " * 100
        dedup.remember_content(content)

        mock_redis.zadd.assert_called_once()
        assert mock_redis.zadd.call_args[0][0] == "dedup:simhash"

    def test_skips_for_short_content(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember_content with short text  →  Then: no Redis call"""
        dedup.remember_content("short")

        mock_redis.zadd.assert_not_called()

    def test_skips_for_empty_content(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """When: remember_content with empty text  →  Then: no Redis call"""
        dedup.remember_content("")

        mock_redis.zadd.assert_not_called()

    def test_handles_redis_error(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: Redis fails  →  When: remember_content  →  Then: no exception"""
        mock_redis.zadd.side_effect = redis.ConnectionError("fail")
        content = "word " * 100

        # Should not raise
        dedup.remember_content(content)


# ---------------------------------------------------------------------------
# Pure helpers — normalize_title, jaccard_similarity
# ---------------------------------------------------------------------------

class TestNormalizeTitle:
    def test_lowercases_and_strips_punctuation(self) -> None:
        assert Deduplicator._normalize_title("Fed Raises Rates! by 25%") == "fed raises rates by 25"

    def test_collapses_multiple_spaces(self) -> None:
        assert Deduplicator._normalize_title("hello   world") == "hello world"

    def test_empty_title(self) -> None:
        assert Deduplicator._normalize_title("") == ""

    def test_none_title(self) -> None:
        assert Deduplicator._normalize_title("") == ""


class TestJaccardSimilarity:
    def test_identical_strings(self) -> None:
        assert Deduplicator._jaccard_similarity("a b c", "a b c") == 1.0

    def test_no_overlap(self) -> None:
        assert Deduplicator._jaccard_similarity("a b", "c d") == 0.0

    def test_partial_overlap(self) -> None:
        # {a,b} ∩ {b,c} = {b}, union = {a,b,c} → 1/3
        result = Deduplicator._jaccard_similarity("a b", "b c")
        assert abs(result - 1 / 3) < 1e-9

    def test_empty_set_returns_zero(self) -> None:
        assert Deduplicator._jaccard_similarity("", "a b") == 0.0
        assert Deduplicator._jaccard_similarity("a b", "") == 0.0


# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------

class TestConfigurableThresholds:
    def test_custom_hamming_threshold(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: hamming_threshold=1  →  Then: distance 2 is not duplicate"""
        from simhash import Simhash

        dedup = Deduplicator(mock_redis, hamming_threshold=1)
        text = "word " * 100
        sh = Simhash(text)
        # Flip 2 bits → distance 2 > threshold 1
        near_hash = sh.value ^ 0b11
        mock_redis.zrangebyscore.return_value = [str(near_hash)]

        item = _make_item(content=text)
        assert dedup.is_duplicate(item) is False

    def test_custom_short_text_threshold(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: short_text_threshold=100  →  Then: 150-char text uses SimHash"""
        dedup = Deduplicator(mock_redis, short_text_threshold=100)
        text = "a" * 150  # > 100 threshold
        item = _make_item(content=text)

        dedup.is_duplicate(item)
        mock_redis.zrangebyscore.assert_called_once()

    def test_custom_title_jaccard_threshold(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: jaccard_threshold=0.5  →  Then: lower overlap still matches"""
        dedup = Deduplicator(mock_redis, title_jaccard_threshold=0.5)
        # "a b c d" vs "a b e f" → jaccard = 2/6 ≈ 0.33 < 0.5
        # "a b c d" vs "a b c e" → jaccard = 3/5 = 0.6 ≥ 0.5
        mock_redis.smembers.return_value = {"a b c e"}
        item = _make_item(title="A B C D", summary=None, content=None)

        assert dedup.is_duplicate(item) is True

    def test_custom_ttl(
        self, mock_redis: MagicMock
    ) -> None:
        """Given: ttl_hours=24  →  Then: setex uses 24*3600"""
        dedup = Deduplicator(mock_redis, ttl_hours=24)
        item = _make_item()
        dedup.remember(item)

        mock_redis.setex.assert_called_once_with(
            "dedup:url:abc123def456", 24 * 3600, "1"
        )


# ---------------------------------------------------------------------------
# Content priority: content > summary > title
# ---------------------------------------------------------------------------

class TestContentPriority:
    def test_uses_content_over_summary(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: both content and summary present  →  Then: content is used for SimHash"""
        long_content = "word " * 100
        item = _make_item(content=long_content, summary="short summary")

        dedup.remember(item)
        # zadd called (SimHash from long content), not sadd (title)
        mock_redis.zadd.assert_called_once()
        mock_redis.sadd.assert_not_called()

    def test_falls_back_to_summary_when_no_content(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: no content, long summary  →  Then: summary used for SimHash"""
        long_summary = "word " * 100
        item = _make_item(content=None, summary=long_summary)

        dedup.remember(item)
        mock_redis.zadd.assert_called_once()

    def test_falls_back_to_title_when_no_content_or_summary(
        self, dedup: Deduplicator, mock_redis: MagicMock
    ) -> None:
        """Given: no content, no summary  →  Then: title used (short text → Jaccard)"""
        item = _make_item(content=None, summary=None)

        dedup.remember(item)
        # Title is short → sadd, not zadd
        mock_redis.sadd.assert_called_once()
        mock_redis.zadd.assert_not_called()
