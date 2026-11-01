import pytest

from codepilot.analysis import analyse_repository
from codepilot.errors import SymbolNotFoundError
from codepilot.graphs import build_graphs
from codepilot.ingestion import scan


@pytest.fixture
def graphs(small_repo):
    return build_graphs(analyse_repository(small_repo, scan(small_repo)))


def test_cross_module_call_edges(graphs):
    edges = set(graphs.calls.edges)
    assert ("shop.pricing:total", "shop.tax:add_tax") in edges          # from .tax import add_tax
    assert ("shop.pricing:total", "shop.pricing:subtotal") in edges     # same module
    assert ("shop.tax:preview", "shop.pricing:subtotal") in edges       # from shop import pricing
    assert ("shop.cart:Cart.checkout", "shop.pricing:total") in edges   # import shop.pricing as p
    assert ("shop.cart:quick", "shop.tax:add_tax") in edges             # aliased from-import
    assert ("shop.cart:Cart.add", "shop.cart:Cart._validate") in edges  # self.method
    assert ("shop.cart:quick", "shop.cart:Cart.__init__") in edges      # class call -> __init__


def test_unresolved_calls_are_kept_as_external(graphs):
    assert "sum" in graphs.external_calls["shop.pricing:subtotal"]
    assert "c.add" in graphs.external_calls["shop.cart:quick"]   # instance dispatch is not guessed


def test_module_graph_and_cycle(graphs):
    assert ("shop.pricing", "shop.tax") in graphs.modules.edges
    assert ("shop.tax", "shop.pricing") in graphs.modules.edges
    assert ["shop.pricing", "shop.tax"] in graphs.module_cycles()


def test_recursion_detected(graphs):
    assert graphs.recursive_functions() == ["shop.cart:fact"]


def test_change_impact_is_transitive(graphs):
    report = graphs.impact("shop.tax:add_tax")
    got = {a["qualname"]: a["distance"] for a in report["affected_functions"]}
    assert got["shop.pricing:total"] == 1
    assert got["shop.cart:quick"] == 1
    assert got["shop.cart:Cart.checkout"] == 2
    assert "shop.cart" in report["affected_modules"]
    limited = graphs.impact("shop.tax:add_tax", max_depth=1)
    assert "shop.cart:Cart.checkout" not in {a["qualname"] for a in limited["affected_functions"]}


def test_unknown_symbol_raises(graphs):
    with pytest.raises(SymbolNotFoundError):
        graphs.impact("shop.nope:missing")


def test_to_dict_is_serialisable(graphs):
    import json
    json.dumps(graphs.to_dict())
