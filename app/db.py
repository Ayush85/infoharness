"""Database models and session helpers (SQLAlchemy 2.0)."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        create_engine, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Base(DeclarativeBase):
    pass


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(2048))
    root: Mapped[str] = mapped_column(String(2048), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|failed
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pages: Mapped[list["Page"]] = relationship(back_populates="scan", cascade="all, delete-orphan")
    issues: Mapped[list["Issue"]] = relationship(back_populates="scan", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(512), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    inbound_count: Mapped[int] = mapped_column(Integer, default=0)
    from_sitemap: Mapped[bool] = mapped_column(Boolean, default=False)
    last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scan: Mapped[Scan] = relationship(back_populates="pages")


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(8))  # high|medium|low
    title: Mapped[str] = mapped_column(String(512))
    detail: Mapped[str] = mapped_column(Text, default="")
    page_a: Mapped[str] = mapped_column(String(2048), default="")
    page_b: Mapped[str] = mapped_column(String(2048), default="")
    quote_a: Mapped[str] = mapped_column(Text, default="")
    quote_b: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    points: Mapped[float] = mapped_column(Float, default=0.0)

    scan: Mapped[Scan] = relationship(back_populates="issues")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    plan: Mapped[str] = mapped_column(String(16), default="free")  # free|pro
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sites: Mapped[list["Site"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class LoginToken(Base):
    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    client_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Site(Base):
    """A docs site a user wants watched: rescanned on a schedule, emailed when new problems appear."""
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    interval_days: Mapped[int] = mapped_column(Integer, default=30)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_scan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|failed
    last_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="sites")


class SiteScan(Base):
    """Links a scan to the monitored site that triggered it (kept separate so `scans` never changes shape)."""
    __tablename__ = "site_scans"

    scan_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, index=True)


class ScanMeta(Base):
    """Per-scan settings that depend on who asked (kept apart from `scans` so that table never changes shape)."""
    __tablename__ = "scan_meta"

    scan_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_pages: Mapped[int] = mapped_column(Integer)


class Waitlist(Base):
    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_scans_status_created", Scan.status, Scan.created_at)

_engine = None
_Session = None


def init_engine(url: str | None = None):
    global _engine, _Session
    url = url or config.settings.database_url
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    # API and worker processes start at the same time; serialise table creation so they don't race.
    with _engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(7340221)"))
        Base.metadata.create_all(conn)
    return _engine


def engine():
    if _engine is None:
        init_engine()
    return _engine


@contextmanager
def session_scope():
    if _Session is None:
        init_engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
