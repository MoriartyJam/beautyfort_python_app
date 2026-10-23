from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShopConnection(Base):
    __tablename__ = "shop_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    encrypted_access_token: Mapped[str] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PricingRule(Base):
    __tablename__ = "pricing_rules"
    __table_args__ = (UniqueConstraint("min_price", "max_price", name="uq_pricing_range"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    min_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    vat_percent: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=20)
    margin_percent: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=15)
    payment_fee_percent: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=3)
    payment_fee_fixed: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_domain: Mapped[str] = mapped_column(String(255), index=True)
    mode: Mapped[str] = mapped_column(String(20), default="preview")
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supplier_count: Mapped[int] = mapped_column(Integer, default=0)
    shopify_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    changed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    csv_path: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list["SyncItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class SyncItem(Base):
    __tablename__ = "sync_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str | None] = mapped_column(String(100))
    variant_id: Mapped[str | None] = mapped_column(String(100))
    inventory_item_id: Mapped[str | None] = mapped_column(String(100))
    sku: Mapped[str] = mapped_column(String(255), index=True)
    supplier_code: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    old_quantity: Mapped[int | None] = mapped_column(Integer)
    new_quantity: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30))
    result: Mapped[str] = mapped_column(String(30), default="pending")
    message: Mapped[str | None] = mapped_column(Text)
    run: Mapped[SyncRun] = relationship(back_populates="items")
