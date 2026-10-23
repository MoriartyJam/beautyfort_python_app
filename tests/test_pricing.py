from decimal import Decimal
from types import SimpleNamespace

from app.pricing import calculate_price


def test_beautyfort_historical_rounding_to_95():
    rule = SimpleNamespace(
        id=1,
        enabled=True,
        min_price=Decimal("10.01"),
        max_price=Decimal("35"),
        vat_percent=Decimal("20"),
        margin_percent=Decimal("15"),
        payment_fee_percent=Decimal("3"),
        payment_fee_fixed=Decimal("2"),
    )
    result = calculate_price(Decimal("20"), [rule])
    assert result.value == Decimal("29.95")


def test_missing_price_band_requires_review():
    result = calculate_price(Decimal("1000"), [])
    assert result.value is None
    assert result.error
