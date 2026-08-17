import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import redis

from app.model_config import ModelRuntimeConfig, resolve_model_runtime_config

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"


def load_yaml(filename: str) -> dict:
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Config:
    """Singleton configuration loaded from YAML files + environment variables."""

    _instance: "Config | None" = None

    def __new__(cls) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        # YAML configs
        self.settings: dict = load_yaml("settings.yaml")
        self.rss_sources: dict = load_yaml("rss_sources.yaml")
        self.watchlist: dict = load_yaml("watchlist.yaml")
        self.prompts: dict = load_yaml("prompts.yaml")

        # Database — env var name comes from settings, fallback for local dev
        db_env_key: str = self.settings["database"]["url_env"]
        db_url: str = os.getenv(db_env_key, "postgresql://postgres:postgres@localhost:5432/news")
        self.engine = create_engine(db_url, pool_pre_ping=True)
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Redis — same pattern
        redis_env_key: str = self.settings["redis"]["url_env"]
        redis_url: str = os.getenv(redis_env_key, "redis://localhost:6379/0")
        self.redis_client = redis.from_url(redis_url, decode_responses=True)

        # Small and large models can use independent OpenAI-compatible APIs.
        self.small_model_config: ModelRuntimeConfig = resolve_model_runtime_config(
            self.settings["small_model"]
        )
        self.large_model_config: ModelRuntimeConfig = resolve_model_runtime_config(
            self.settings["large_model"]
        )


config = Config()
