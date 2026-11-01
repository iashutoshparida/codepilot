import textwrap
from pathlib import Path

import pytest


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return root


@pytest.fixture
def small_repo(tmp_path):
    """A tiny package with cross-module calls, a class, recursion and an import cycle."""
    return write_tree(tmp_path, {
        "shop/__init__.py": "",
        "shop/pricing.py": """
            from .tax import add_tax

            def subtotal(items):
                return sum(i["price"] * i["qty"] for i in items)

            def total(items):
                return add_tax(subtotal(items))
        """,
        "shop/tax.py": """
            from shop import pricing

            RATE = 0.18

            def add_tax(amount):
                return round(amount * (1 + RATE), 2)

            def preview(items):
                return pricing.subtotal(items)
        """,
        "shop/cart.py": """
            import shop.pricing as p
            from shop.tax import add_tax as tax

            class Cart:
                def __init__(self):
                    self.items = []

                def add(self, name, price, qty=1):
                    self._validate(price, qty)
                    self.items.append({"name": name, "price": price, "qty": qty})

                def _validate(self, price, qty):
                    if price < 0 or qty <= 0:
                        raise ValueError("bad item")

                def checkout(self):
                    return p.total(self.items)

            def quick(price):
                c = Cart()
                c.add("x", price)
                return tax(price)

            def fact(n):
                return 1 if n <= 1 else n * fact(n - 1)
        """,
        "tests/test_cart.py": """
            from shop.cart import Cart

            def test_checkout():
                c = Cart()
                c.add("a", 100, 2)
                assert c.checkout() == 236.0
        """,
    })
