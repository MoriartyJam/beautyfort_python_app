from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppSetting, PricingRule


DEFAULT_RULES = [
    ("0", "10", "20", "30", "3", "0.20"),
    ("10.01", "35", "20", "15", "3", "2"),
    ("35.01", "50", "20", "20", "3", "3"),
    ("50.01", "100", "20", "17.5", "3", "4"),
    ("100.01", "150", "20", "15", "3", "5"),
    ("150.01", "999", "20", "12.5", "3", "6"),
]

DEFAULT_SETTINGS = {
    "sku_prefix": "BR",
    "minimum_supplier_stock": "0",
    "missing_product_action": "zero",
    "max_price_change_percent": "1000000",
    "max_items_per_apply": "10000",
    # Scheduled synchronization is opt-in in production. Manual "Update now"
    # jobs still apply their reviewed configuration when explicitly requested.
    "automatic_apply": "false",
}


def seed_defaults(db: Session) -> None:
    if not db.scalar(select(PricingRule.id).limit(1)):
        for values in DEFAULT_RULES:
            db.add(
                PricingRule(
                    min_price=Decimal(values[0]),
                    max_price=Decimal(values[1]),
                    vat_percent=Decimal(values[2]),
                    margin_percent=Decimal(values[3]),
                    payment_fee_percent=Decimal(values[4]),
                    payment_fee_fixed=Decimal(values[5]),
                )
            )
    for key, value in DEFAULT_SETTINGS.items():
        if not db.get(AppSetting, key):
            db.add(AppSetting(key=key, value=value))
    db.commit()


def get_app_settings(db: Session) -> dict[str, str]:
    return {item.key: item.value for item in db.scalars(select(AppSetting)).all()}
