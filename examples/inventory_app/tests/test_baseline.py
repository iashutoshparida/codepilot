"""The original, thin test suite that came with the legacy code."""
from inventory.orders import validate_sku
from inventory.pricing import add_gst
from inventory.stock import add_stock


def test_add_stock():
    assert add_stock(10, 5) == 15


def test_gst():
    assert add_gst(100.0) == 118.0


def test_valid_sku():
    assert validate_sku("ABC-1234")
