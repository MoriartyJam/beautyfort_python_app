from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .models import PricingRule


@dataclass(frozen=True)
class PriceResult:
    value: Decimal | None
    rule_id: int | None
    error: str | None = None


def calculate_price(base_price: Decimal, rules: list[PricingRule]) -> PriceResult:
    rule = next(
        (
            item
            for item in rules
            if item.enabled and Decimal(item.min_price) <= base_price <= Decimal(item.max_price)
        ),
        None,
    )
    if not rule:
        return PriceResult(None, None, "No pricing rule covers this supplier price")

    percent = Decimal(rule.vat_percent) + Decimal(rule.margin_percent) + Decimal(rule.payment_fee_percent)
    raw = base_price + (base_price * percent / Decimal("100")) + Decimal(rule.payment_fee_fixed)
    # Preserve Beautyfort's historical pricing convention: whole pounds + .95.
    result = raw.quantize(Decimal("1"), rounding=ROUND_FLOOR) + Decimal("0.95")
    return PriceResult(result.quantize(Decimal("0.01")), rule.id)
