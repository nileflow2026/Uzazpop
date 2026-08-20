"""

Sets up the SQLAlchemy async-compatible engine and session factory for SQLite.

DESIGN DECISIONS:
  • SQLite with check_same_thread=False  → FastAPI runs handlers in a thread pool;
    the flag is safe because SQLAlchemy manages its own connection-per-session.
  • connect_args["timeout"] = 30          → Prevents indefinite lock waits; raises
    OperationalError instead of hanging.
  • echo=DEBUG                            → SQL logging only in debug mode to avoid
    leaking sensitive data in production logs.

RISKS MITIGATED:
  • SQLite WAL mode enabled via event listener → dramatically reduces "database is
    locked" errors under concurrent reads + writes.
  • Foreign key enforcement ON            → SQLite disables FK checks by default;
    this re-enables them per connection.
"""

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from typing import Generator

from config import get_settings

settings = get_settings()


def _resolve_database_url(raw_url: str) -> str:
    """
    A relative SQLite path (e.g. 'sqlite:///./pharmacy_pos.db') resolves
    against the process's CURRENT WORKING DIRECTORY at startup — NOT the
    project folder. That directory silently changes depending on how the
    server is launched (terminal vs. an IDE's run button, different run
    configurations, etc.), which means each different launch method can
    end up pointing at a different, brand-new, empty database file —
    with your real data untouched but orphaned in the old location. It
    LOOKS like data loss, but it's actually just multiple database files.

    Fix: anchor any relative sqlite path to this file's own directory
    (the project root), so the database file is always the same
    physical file no matter how or from where the app is started.
    Absolute sqlite paths and non-sqlite URLs (e.g. Render's Postgres)
    are left untouched.
    """
    if not raw_url.startswith("sqlite:///"):
        return raw_url  # Not SQLite (e.g. postgresql://...) — leave as-is

    path_part = raw_url[len("sqlite:///"):]
    if path_part.startswith("/"):
        return raw_url  # Already an absolute path — leave as-is

    project_root = Path(__file__).resolve().parent
    absolute_path = (project_root / path_part.lstrip("./")).resolve()
    return f"sqlite:///{absolute_path}"


DATABASE_URL = _resolve_database_url(settings.DATABASE_URL)

# Never let a deployed service silently create an ephemeral SQLite database.
# Production must use the Render-managed PostgreSQL database so all devices
# share the same users and transactions.
if not settings.DEBUG and DATABASE_URL.startswith("sqlite"):
    raise RuntimeError(
        "Production DATABASE_URL is SQLite or missing. Configure Render's "
        "DATABASE_URL with the PostgreSQL connection string before deploying."
    )


# ── Engine ────────────────────────────────────────────────────────────────────
# connect_args with check_same_thread is SQLite-only.
# For PostgreSQL (Render) we pass no connect_args.
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)
@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_conn, _connection_record):
    """
    Called once per new raw DBAPI connection.
    PRAGMA commands are SQLite-only — skip entirely for PostgreSQL.
    """
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

# ── Session factory ───────────────────────────────────────────────────────────

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,   # Explicit transaction management
    autoflush=False,    # Flush only when we call .commit() or explicitly .flush()
    expire_on_commit=False,  # Keep objects usable after commit (important for FastAPI responses)
)


# ── Base declarative class ────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """All ORM models inherit from this class."""
    pass


# ── Dependency ────────────────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session and guarantees cleanup.

    Usage in a router:
        db: Session = Depends(get_db)

    The try/finally ensures the session is always closed even if an exception
    is raised mid-request, preventing connection leaks.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()  # Roll back any uncommitted transaction on error
        raise  # Re-raise so FastAPI can handle it normally
    finally:
        db.close()  # Always close to return connection to pool