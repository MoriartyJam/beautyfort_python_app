import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .beautyfort import BeautyfortClient, SupplierProduct
from .config import Settings
from .defaults import get_app_settings
from .models import PricingRule, SyncItem, SyncRun
from .pricing import calculate_price
from .shopify import ShopifyClient, ShopifyError, ShopifyVariant


logger = logging.getLogger(__name__)
def supplier_key(sku: str, prefix: str) -> str:
    normalized = sku.strip().upper()
    prefix = prefix.strip().upper()
    return normalized[len(prefix):] if prefix and normalized.startswith(prefix) else normalized


def build_item(
    run_id: int,
    variant: ShopifyVariant,
    supplier: SupplierProduct | None,
    rules: list[PricingRule],
    minimum_stock: int,
    missing_action: str,
    max_price_change_percent: Decimal,
) -> SyncItem:
    base = dict(
        run_id=run_id,
        product_id=variant.product_id,
        variant_id=variant.variant_id,
        inventory_item_id=variant.inventory_item_id,
        sku=variant.sku,
        supplier_code=supplier.stock_code if supplier else None,
        title=variant.title,
        old_price=variant.price,
        old_quantity=variant.quantity,
    )
    if supplier is None:
        if missing_action == "zero":
            return SyncItem(
                **base,
                new_price=variant.price,
                new_quantity=0,
                action="zero_missing" if variant.quantity != 0 else "none",
                result="preview" if variant.quantity != 0 else "unchanged",
            )
        return SyncItem(**base, new_price=None, new_quantity=None, action="review_missing", result="skipped")

    if supplier.stock_level < minimum_stock:
        return SyncItem(
            **base,
            new_price=variant.price,
            new_quantity=0,
            action="zero_low_stock",
            result="preview" if variant.quantity != 0 else "unchanged",
            message=f"Supplier stock {supplier.stock_level} is below minimum {minimum_stock}",
        )

    calculated = calculate_price(supplier.price, rules)
    if calculated.value is None:
        return SyncItem(
            **base,
            new_price=None,
            new_quantity=supplier.stock_level,
            action="review_price_rule",
            result="skipped",
            message=calculated.error,
        )

    if variant.price > 0:
        change = abs(calculated.value - variant.price) / variant.price * Decimal("100")
        if change > max_price_change_percent:
            return SyncItem(
                **base,
                new_price=calculated.value,
                new_quantity=supplier.stock_level,
                action="review_price_change",
                result="skipped",
                message=f"Price change {change:.1f}% exceeds safety limit {max_price_change_percent}%",
            )

    changed = calculated.value != variant.price or supplier.stock_level != variant.quantity
    return SyncItem(
        **base,
        new_price=calculated.value,
        new_quantity=supplier.stock_level,
        action="update" if changed else "none",
        result="preview" if changed else "unchanged",
    )


async def create_preview(
    db: Session,
    config: Settings,
    shop: str,
    token: str,
    run_id: int | None = None,
) -> SyncRun:
    if run_id is None:
        running = db.scalar(select(SyncRun).where(SyncRun.status == "running").limit(1))
        if running:
            raise RuntimeError(f"Synchronization #{running.id} is already running")
        run = SyncRun(shop_domain=shop, mode="preview", status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
    else:
        run = db.get(SyncRun, run_id)
        if not run or run.shop_domain != shop or run.status != "running":
            raise RuntimeError("Queued synchronization was not found or was not claimed")

    try:
        values = get_app_settings(db)
        prefix = values.get("sku_prefix", "BR")
        minimum_stock = int(values.get("minimum_supplier_stock", "5"))
        missing_action = values.get("missing_product_action", "review")
        max_change = Decimal(values.get("max_price_change_percent", "40"))
        rules = list(db.scalars(select(PricingRule).order_by(PricingRule.min_price)).all())

        supplier_rows = await BeautyfortClient(config).fetch_stock()
        shopify = ShopifyClient(config, shop, token)
        variants = await shopify.fetch_variants(prefix)
        supplier_map = {row.stock_code.strip().upper(): row for row in supplier_rows}

        for variant in variants:
            supplier = supplier_map.get(supplier_key(variant.sku, prefix))
            db.add(build_item(run.id, variant, supplier, rules, minimum_stock, missing_action, max_change))
        db.commit()

        items = list(db.scalars(select(SyncItem).where(SyncItem.run_id == run.id)).all())
        run.supplier_count = len(supplier_rows)
        run.shopify_count = len(variants)
        run.matched_count = sum(item.supplier_code is not None for item in items)
        run.changed_count = sum(item.action in {"update", "zero_low_stock", "zero_missing"} and item.result == "preview" for item in items)
        run.skipped_count = sum(item.result in {"skipped", "unchanged"} for item in items)
        run.status = "preview_ready"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(SyncRun, run.id)
        run.status = "failed"
        run.error_message = str(exc)[:4000]
        run.completed_at = datetime.now(timezone.utc)
        run.error_count = 1
        db.commit()
        logger.exception("Preview run %s failed", run.id)
        raise


async def apply_preview(db: Session, config: Settings, run_id: int, shop: str, token: str) -> SyncRun:
    run = db.get(SyncRun, run_id)
    if not run or run.shop_domain != shop:
        raise RuntimeError("Preview was not found")
    if run.status not in {"preview_ready", "running"}:
        raise RuntimeError("Only a completed preview can be applied")

    values = get_app_settings(db)
    limit = int(values.get("max_items_per_apply", "500"))
    items = list(
        db.scalars(
            select(SyncItem)
            .where(SyncItem.run_id == run.id, SyncItem.result == "preview")
            .order_by(SyncItem.id)
        ).all()
    )
    if len(items) > limit:
        raise RuntimeError(f"Safety limit: {len(items)} changes exceed max_items_per_apply={limit}")

    run.mode = "apply"
    run.status = "running"
    run.completed_at = None
    db.commit()
    client = ShopifyClient(config, shop, token)
    location_id = await client.primary_location_id()
    errors = 0
    for item in items:
        try:
            await client.update_variant(
                product_id=item.product_id or "",
                variant_id=item.variant_id or "",
                inventory_item_id=item.inventory_item_id or "",
                old_quantity=item.old_quantity or 0,
                new_price=Decimal(item.new_price if item.new_price is not None else item.old_price),
                new_quantity=int(item.new_quantity if item.new_quantity is not None else item.old_quantity or 0),
                location_id=location_id,
                idempotency_key=f"beautyfort-{run.id}-{item.id}-{uuid4().hex[:8]}",
            )
            item.result = "updated"
        except ShopifyError as exc:
            item.result = "failed"
            item.message = str(exc)[:2000]
            errors += 1
        db.commit()

    run.error_count = errors
    run.status = "completed_with_errors" if errors else "completed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run
