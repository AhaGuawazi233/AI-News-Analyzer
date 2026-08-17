import os

from celery import Celery
from celery.signals import worker_process_init
from celery.schedules import crontab

from app.config import config


# Initialize database tables when worker process starts
@worker_process_init.connect
def _init_db_on_worker_start(**kwargs):
    """Create database tables when Celery worker process initializes."""
    from app.db import init_db
    init_db()


# Resolve broker URL from env var name stored in settings
_broker_url: str = os.getenv(
    config.settings["redis"]["url_env"],
    "redis://localhost:6379/0",
)

celery_app = Celery(
    "news_analyzer",
    broker=_broker_url,
    # No result backend — we persist to PostgreSQL directly
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Timezone
    timezone="Asia/Shanghai",
    enable_utc=True,
    # Reliability — ack after task completes, not on receive
    task_track_started=True,
    task_acks_late=True,
    # One task at a time per worker process
    worker_prefetch_multiplier=1,
    # Routing — each task type gets its own queue
    task_routes={
        "collect": {"queue": "collect"},
        "fetch": {"queue": "fetch"},
        "classify": {"queue": "classify"},
        "analyze": {"queue": "analyze"},
        "notify": {"queue": "notify"},
    },
    task_default_queue="default",
    # Beat schedule — periodic tasks
    beat_schedule={
        "collect-news": {
            "task": "collect",
            "schedule": crontab(minute="*/3"),
        },
    },
)

# Auto-discover task modules in app/
celery_app.autodiscover_tasks(["app"])
