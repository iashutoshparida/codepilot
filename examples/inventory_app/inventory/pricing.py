"""Discounts and tax."""


def bulk_discount_pct(qty: int) -> int:
    if qty >= 100:
        return 10
    if qty >= 50:
        return 5
    return 0


def apply_discount(price: float, pct: float) -> float:
    if pct < 0 or pct > 100:
        raise ValueError("discount must be between 0 and 100")
    return round(price * (1 - pct / 100), 2)


def add_gst(amount: float, rate: float = 0.18) -> float:
    return round(amount * (1 + rate), 2)


def line_total(unit_price: float, qty: int) -> float:
    if qty <= 0:
        raise ValueError("quantity must be positive")
    gross = unit_price * qty
    return add_gst(apply_discount(gross, bulk_discount_pct(qty)))
