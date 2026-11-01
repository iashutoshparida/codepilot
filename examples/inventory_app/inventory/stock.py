"""Stock levels for a single SKU."""


class InsufficientStock(Exception):
    pass


def add_stock(level: int, qty: int) -> int:
    if qty <= 0:
        raise ValueError("quantity must be positive")
    return level + qty


def remove_stock(level: int, qty: int) -> int:
    if qty <= 0:
        raise ValueError("quantity must be positive")
    if qty > level:
        raise InsufficientStock(f"only {level} left")
    return level - qty


def reorder_needed(level: int, reorder_point: int) -> bool:
    return level <= reorder_point


def days_of_cover(level: int, daily_demand: int) -> float:
    # Legacy: crashes when there is no demand yet (pre-existing bug, left in on purpose)
    return round(level / daily_demand, 1)


def safety_stock(daily_demand: int, lead_time_days: int, buffer_pct: int) -> int:
    base = daily_demand * lead_time_days
    return base + (base * buffer_pct) // 100
