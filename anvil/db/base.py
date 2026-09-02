"""Declarative base, shared column types and mixins.

The custom types here exist to make the financial invariants structural rather
than advisory: money is stored as a composite of integer minor units plus a
currency, so there is no column anywhere in the schema that a float could be
written into.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, MetaData, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from anvil.domain.money import Currency

#: Explicit naming so Alembic autogenerate produces stable, reviewable names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[dict[str, Any]]: JSONB,
        dt.datetime: DateTime(timezone=True),
    }

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} {pk}>"


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Refuses naive datetimes on the way in; guarantees UTC on the way out."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; Anvil stores UTC-aware instants only")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


class CurrencyType(TypeDecorator[Currency]):
    impl = String(3)
    cache_ok = True

    def process_bind_param(self, value: Currency | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return Currency(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> Currency | None:
        return None if value is None else Currency(value)


def pk_column(prefix_hint: str) -> Mapped[str]:
    """Primary key column. Ids are prefixed ULIDs minted in the application."""
    return mapped_column(
        String(32), primary_key=True, comment=f"prefixed ULID, e.g. {prefix_hint}_01J..."
    )


def money_minor() -> Mapped[int]:
    """A signed integer count of minor units. Never a float, never a numeric."""
    return mapped_column(BigInteger, nullable=False)


def currency_col(default: Currency = Currency.INR) -> Mapped[Currency]:
    return mapped_column(
        CurrencyType, nullable=False, default=default, server_default=default.value
    )


class TimestampMixin:
    """Creation and update instants, set by the database."""

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=sa.func.now(), index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
    )


class CreatedAtMixin:
    """Creation instant only. Used by append-only tables, which never update."""

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, server_default=sa.func.now(), index=True
    )


class VersionMixin:
    """Optimistic locking. SQLAlchemy raises StaleDataError on a version mismatch,
    which is how two operators are stopped from resolving one approval twice."""

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": None}  # overridden per-model below


def versioned_mapper_args(cls: type[Any]) -> dict[str, Any]:
    return {"version_id_col": cls.version}


class MerchantScopedMixin:
    """Everything multi-tenant carries its merchant, and every query filters on it."""

    merchant_id: Mapped[str] = mapped_column(
        String(32), sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
