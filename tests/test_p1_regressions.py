"""Regression tests for the P1 findings fixed in August 2026."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy.sql.elements import TextClause

from app import net_safety
from app.api import require_api_token
from app.net_safety import UnsafeUrlError, safe_get, validate_public_http_url
from app.models import HistoryRepository
from app.schemas import NewsItem


def _resolver_for(address: str):
    def resolve(*args, **kwargs):
        return [(2, 1, 6, "", (address, 443))]

    return resolve


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"],
)
def test_url_validation_rejects_non_public_addresses(address: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_http_url(
            "https://news.example/article",
            resolver=_resolver_for(address),
        )


def test_url_validation_accepts_public_addresses() -> None:
    assert (
        validate_public_http_url(
            "https://news.example/article",
            resolver=_resolver_for("8.8.8.8"),
        )
        == "https://news.example/article"
    )


def test_safe_get_validates_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    checked_urls: list[str] = []

    def validate(url: str) -> str:
        checked_urls.append(url)
        if url.startswith("http://127.0.0.1"):
            raise UnsafeUrlError("loopback target")
        return url

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def get(self, url: str, **kwargs) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/admin"},
                request=request,
            )

    monkeypatch.setattr(net_safety, "validate_public_http_url", validate)
    monkeypatch.setattr(net_safety.httpx, "Client", FakeClient)

    with pytest.raises(UnsafeUrlError):
        safe_get("https://news.example/article", timeout=1)

    assert checked_urls == [
        "https://news.example/article",
        "http://127.0.0.1/admin",
    ]


def test_safe_get_rejects_non_public_connected_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStream:
        def get_extra_info(self, name: str):
            assert name == "server_addr"
            return ("127.0.0.1", 80)

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

        def get(self, url: str, **kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                extensions={"network_stream": FakeStream()},
            )

    monkeypatch.setattr(net_safety, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(net_safety.httpx, "Client", FakeClient)

    with pytest.raises(UnsafeUrlError):
        safe_get("https://news.example/article", timeout=1)


def test_history_query_uses_sqlalchemy_text_clause() -> None:
    class FakeResult:
        def fetchall(self):
            return [("Recent title",)]

    class FakeSession:
        statement = None

        def execute(self, statement, params):
            self.statement = statement
            return FakeResult()

        def close(self) -> None:
            pass

    session = FakeSession()
    repository = HistoryRepository(lambda: session)

    assert repository.get_important_titles_last_24h() == ["Recent title"]
    assert isinstance(session.statement, TextClause)


def test_analyzer_prompt_requests_actionable_text() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "config" / "prompts.yaml").open(encoding="utf-8") as handle:
        prompts = yaml.safe_load(handle)

    analyzer_prompt = prompts["analyzer_system"]
    assert '"actionable": true|false' not in analyzer_prompt
    assert "specific investor action" in analyzer_prompt


def test_api_requires_matching_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "test-secret-token")

    require_api_token("Bearer test-secret-token")

    with pytest.raises(HTTPException) as error:
        require_api_token("Bearer wrong-token")
    assert error.value.status_code == 401


def test_api_fails_closed_without_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    with pytest.raises(HTTPException) as error:
        require_api_token(None)
    assert error.value.status_code == 503


def test_collect_dispatches_fetch_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.tasks as tasks

    events: list[str] = []
    item = NewsItem(
        title="Test article",
        url="https://news.example/article",
        source="test",
        source_type="rss",
        collected_at=datetime.now(timezone.utc).isoformat(),
        url_hash="0123456789abcdef",
    )

    class FakeCollector:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self) -> list[NewsItem]:
            return [item]

    class FakeDeduplicator:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def is_duplicate(self, candidate: NewsItem) -> bool:
            return False

        def remember(self, candidate: NewsItem) -> None:
            events.append("remember")

    class FakeQuery:
        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    class FakeSession:
        added = None

        def query(self, model):
            return FakeQuery()

        def add(self, value) -> None:
            self.added = value

        def flush(self) -> None:
            self.added.id = 123

    @contextmanager
    def fake_db_session():
        yield FakeSession()
        events.append("commit")

    fake_config = SimpleNamespace(
        redis_client=object(),
        rss_sources={
            "sources": [
                {
                    "name": "test",
                    "type": "rss",
                    "url": "https://feed.example/rss",
                }
            ]
        },
        settings={
            "dedup": {
                "use_simhash": True,
                "hamming_threshold": 3,
                "short_text_threshold": 200,
                "title_jaccard_threshold": 0.8,
                "ttl_hours": 48,
            },
            "proxy": {"default": ""},
        },
    )

    monkeypatch.setattr(tasks, "config", fake_config)
    monkeypatch.setattr(tasks, "RSSCollector", FakeCollector)
    monkeypatch.setattr(tasks, "Deduplicator", FakeDeduplicator)
    monkeypatch.setattr(tasks, "get_db_session", fake_db_session)
    monkeypatch.setattr(tasks.fetch_task, "delay", lambda item_id: events.append("delay"))

    assert tasks.collect_task.run() == {"collected": 1, "deduped": 0}
    assert events == ["commit", "delay", "remember"]
