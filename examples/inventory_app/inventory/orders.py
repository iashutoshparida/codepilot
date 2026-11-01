"""Order validation and shipping."""
import re

SKU_PATTERN = re.compile(r"^[A-Z]{3}-\d{4}$")


def validate_sku(sku: str) -> bool:
    return bool(SKU_PATTERN.match(sku.strip().upper()))


def parse_quantity(text: str) -> int:
    value = text.strip()
    if not value.isdigit():
        raise ValueError(f"not a whole number: {text!r}")
    qty = int(value)
    if qty == 0 or qty > 10000:
        raise ValueError("quantity out of range")
    return qty


def split_shipments(qty: int, box_size: int) -> list:
    if box_size <= 0:
        raise ValueError("box size must be positive")
    full, rest = divmod(qty, box_size)
    boxes = [box_size] * full
    if rest:
        boxes.append(rest)
    return boxes


def shipping_priority(order_value: float, is_prime: bool) -> str:
    if is_prime and order_value >= 500:
        return "express"
    if is_prime or order_value >= 2000:
        return "priority"
    return "standard"
