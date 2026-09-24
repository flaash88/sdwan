"""Datenbank-Setup inkl. strikter Mandanten-Isolation.

Jede Session, die aus einem API-Request stammt, trägt in ``session.info["tenant_scope"]``
den aktiven Tenant. Ein ``do_orm_execute``-Hook hängt an *jede* ORM-Query
(SELECT/UPDATE/DELETE) automatisch ``WHERE tenant_id = :tenant`` für alle Modelle mit
``TenantScoped``-Mixin an. Ein ``before_flush``-Hook verhindert, dass Objekte eines
fremden Tenants geschrieben werden. Damit ist ein vergessener Filter im Endpoint-Code
kein Datenleck.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, MetaData, event, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column, with_loader_criteria
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

ALL_TENANTS = "__all__"

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """Speichert immer timezone-aware UTC (auch unter SQLite in Tests)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TenantScoped:
    """Mixin: Datensatz gehört genau einem Tenant."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


class GlobalOrTenantScoped:
    """Mixin: Datensatz gehört einem Tenant oder ist global (tenant_id NULL, nur MSP pflegt)."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True)


class TenantIsolationError(PermissionError):
    pass


@event.listens_for(Session, "do_orm_execute")
def _tenant_filter(state: Any) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.execution_options.get("skip_tenant_filter", False):
        return
    scope = state.session.info.get("tenant_scope")
    if scope is None or scope == ALL_TENANTS:
        return
    tenant_id = scope
    state.statement = state.statement.options(
        with_loader_criteria(TenantScoped, lambda cls: cls.tenant_id == tenant_id, include_aliases=True),
        with_loader_criteria(
            GlobalOrTenantScoped,
            lambda cls: or_(cls.tenant_id == tenant_id, cls.tenant_id.is_(None)),
            include_aliases=True,
        ),
    )


@event.listens_for(Session, "before_flush")
def _tenant_guard(session: Session, flush_context: Any, instances: Any) -> None:
    scope = session.info.get("tenant_scope")
    if scope is None or scope == ALL_TENANTS:
        return
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, TenantScoped):
            if obj.tenant_id is None and obj in session.new:
                obj.tenant_id = scope
            elif obj.tenant_id != scope:
                raise TenantIsolationError(f"Cross-tenant write blocked on {type(obj).__name__}")
        elif isinstance(obj, GlobalOrTenantScoped):
            # Tenant-Nutzer dürfen globale Objekte lesen, aber nicht schreiben
            if obj in session.new and obj.tenant_id is None:
                obj.tenant_id = scope
            elif obj.tenant_id != scope:
                raise TenantIsolationError(f"Cross-tenant write blocked on {type(obj).__name__}")


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_async_engine(url, **kwargs)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def reset_engine() -> None:
    """Nur für Tests."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


def system_session() -> AsyncSession:
    """Unscoped Session für Worker/Systemjobs (explizit mandantenübergreifend)."""
    return sessionmaker()()


def tenant_session(tenant_id: uuid.UUID) -> AsyncSession:
    s = sessionmaker()()
    s.info["tenant_scope"] = tenant_id
    return s


async def create_all() -> None:
    import app.models  # noqa: F401  (registriert alle Tabellen)

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
