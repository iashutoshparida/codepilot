"""Legacy reporting helpers. Several maintainability problems are left in on purpose."""
import json
import os
from typing import Dict


def average_order_value(values: list[float]) -> float:
    return round(sum(values) / len(values), 2)


def summarise(orders, seen=[]):
    out = {}
    for o in orders:
        if o:
            if "sku" in o:
                if o["sku"] not in seen:
                    if o.get("qty", 0) > 0:
                        seen.append(o["sku"])
                        out[o["sku"]] = out.get(o["sku"], 0) + o["qty"]
    return out


def load_rules(text):
    try:
        return eval(text)
    except:
        return {}


def export_report(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)


def total_units(orders):
    total = 0
    for o in orders:
        total += o["qty"]
    return total


def units_shipped(orders):
    total = 0
    for o in orders:
        total += o["qty"]
    return total
