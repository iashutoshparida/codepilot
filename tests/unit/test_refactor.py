import pytest

from codepilot.analysis import analyse_repository
from codepilot.assistant import build_proposal
from codepilot.errors import PatchRejectedError
from codepilot.graphs import build_graphs
from codepilot.ingestion import scan
from codepilot.refactor import RefactorWorkflow


@pytest.fixture
def setup(small_repo, tmp_path_factory):
    model = analyse_repository(small_repo, scan(small_repo))
    graphs = build_graphs(model)
    wf = RefactorWorkflow(small_repo, tmp_path_factory.mktemp("state"), ["tests"])
    fn = model.functions()["shop.pricing:subtotal"]
    return small_repo, wf, graphs, fn


GOOD = ("def subtotal(items):\n    total = 0\n    for i in items:\n"
        "        total += i['price'] * i['qty']\n    return total\n")
BAD = "def subtotal(items):\n    return sum(i['price'] for i in items)\n"   # drops qty: a regression


def test_safe_refactor_is_applied_with_backup_and_can_roll_back(setup):
    repo, wf, graphs, fn = setup
    original = (repo / fn.file).read_text()
    rec = wf.propose(build_proposal(repo, fn, GOOD))
    assert rec.status == "pending" and (repo / fn.file).read_text() == original   # nothing applied yet
    rec = wf.approve(rec.id, "ashutosh", graphs)
    assert rec.status == "applied" and rec.validation["regressions"] == []
    assert "for i in items" in (repo / fn.file).read_text()
    affected = {a["qualname"] for a in rec.validation["change_impact"]["affected_functions"]}
    assert "shop.cart:Cart.checkout" in affected
    assert rec.validation["tests_referencing_changed_code"] == ["tests/test_cart.py"]
    rec = wf.rollback(rec.id)
    assert rec.status == "rolled_back" and (repo / fn.file).read_text() == original


def test_regressing_refactor_is_blocked_and_original_untouched(setup):
    repo, wf, graphs, fn = setup
    original = (repo / fn.file).read_text()
    rec = wf.approve(wf.propose(build_proposal(repo, fn, BAD)).id, "ashutosh", graphs)
    assert rec.status == "regressed"
    assert rec.validation["regressions"] == ["tests.test_cart::test_checkout"]
    assert (repo / fn.file).read_text() == original


def test_approval_rules(setup):
    repo, wf, graphs, fn = setup
    rec = wf.propose(build_proposal(repo, fn, GOOD))
    with pytest.raises(PatchRejectedError, match="reviewer"):
        wf.approve(rec.id, "  ")
    (repo / fn.file).write_text((repo / fn.file).read_text() + "\n# edited\n")
    with pytest.raises(PatchRejectedError, match="changed after"):
        wf.approve(rec.id, "ashutosh")
    assert wf.reject(rec.id).status == "rejected"
    with pytest.raises(PatchRejectedError, match="not pending"):
        wf.approve(rec.id, "ashutosh")
    with pytest.raises(PatchRejectedError, match="invalid patch id"):
        wf.store.get("../../etc/passwd")


def test_rollback_refuses_if_file_edited_after_apply(setup):
    repo, wf, graphs, fn = setup
    rec = wf.approve(wf.propose(build_proposal(repo, fn, GOOD)).id, "a", graphs)
    (repo / fn.file).write_text((repo / fn.file).read_text() + "# later edit\n")
    with pytest.raises(PatchRejectedError, match="edited after"):
        wf.rollback(rec.id)
