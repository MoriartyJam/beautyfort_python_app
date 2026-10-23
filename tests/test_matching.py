from app.sync_service import supplier_key


def test_prefixed_sku_maps_to_supplier_stock_code():
    assert supplier_key("BRA00586", "BR") == "A00586"


def test_unprefixed_sku_is_supported_for_legacy_compatibility():
    assert supplier_key("A00586", "BR") == "A00586"
