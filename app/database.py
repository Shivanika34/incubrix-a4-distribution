"""SQLAlchemy engine and tables for jobs and idempotency records."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_links: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("key", name="uq_idempotency_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Destination(Base):
    """A configured publishing destination; adding a row requires no connector code change."""

    __tablename__ = "destinations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    platform_key: Mapped[str] = mapped_column(String(64), nullable=False)
    surface: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128))
    audience_tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    language: Mapped[str | None] = mapped_column(String(32))
    geo: Mapped[str | None] = mapped_column(String(64))
    est_audience: Mapped[int | None] = mapped_column(Integer)
    rules_url: Mapped[str | None] = mapped_column(String(2048))
    frequency_cap: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    self_promo_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    link_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="allow")
    disclosure_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automation_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    connector_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relevance_score: Mapped[int | None] = mapped_column(Integer)
    relevance_reason: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(4), nullable=False, default="L0")
    last_certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuleDecision(Base):
    __tablename__ = "rule_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


def make_database(database_url: str):
    is_sqlite = database_url.startswith("sqlite")
    options = {"connect_args": {"check_same_thread": False}, "poolclass": NullPool} if is_sqlite else {}
    engine = create_engine(database_url, pool_pre_ping=True, **options)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    return engine, factory
