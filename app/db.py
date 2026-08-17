from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.config import config
from app.models import Base


def init_db() -> None:
    """Create all database tables if they don't exist.
    
    Call this once at application startup (API server, worker, beat).
    Safe to call multiple times — CREATE TABLE IF NOT EXISTS.
    """
    Base.metadata.create_all(bind=config.engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Yield a database session; commit on success, rollback on error."""
    session = config.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
