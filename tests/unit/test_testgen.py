from codepilot.analysis import analyse_repository, analyse_source
from codepilot.ingestion import scan
from codepilot.testgen import (
    _kind_from_annotation,
    build_cases,
    classify_failure,
    drop_tests,
    generate_for_module,
    validate_candidate_tests,
)
from tests.conftest import write_tree

CALC = {
    "calc/__init__.py": "",
    "calc/ops.py": """
        class InvalidAmount(Exception):
            pass

        def clamp(value: int, low: int, high: int) -> int:
            if low > high:
                raise ValueError("low > high")
            return max(low, min(value, high))

        def mean(xs: list[int]) -> float:
            return sum(xs) / len(xs)          # crashes on [] - an edge case worth reporting

        def discount(price: float, pct: float) -> float:
            if price < 0:
                raise InvalidAmount("negative")
            return round(price * (1 - pct / 100), 2)

        def greet(name):
            return "Hello, " + name.strip().title()

        def _private(x):
            return x

        def numbers(n):
            yield from range(n)
    """,
}


def test_annotation_kinds():
    assert _kind_from_annotation("int") == "int"
    assert _kind_from_annotation("Optional[str]") == "str?"
    assert _kind_from_annotation("int | None") == "int?"
    assert _kind_from_annotation("list[str]") == "list_str"
    assert _kind_from_annotation("List[int]") == "list_num"
    assert _kind_from_annotation("SomeClass") is None


def test_cases_vary_one_argument_at_a_time():
    fn = analyse_source("def clamp(value: int, low: int, high: int):\n    pass\n", "m", "m.py").functions[0]
    cases = build_cases(fn)
    assert cases[0] == "(3, 3, 3)"
    assert "(0, 3, 3)" in cases and "(3, -1, 3)" in cases and "(3, 3, 100)" in cases
    assert len(cases) == len(set(cases)) <= 15


def test_generate_validate_and_report_edge_cases(tmp_path):
    root = write_tree(tmp_path, CALC)
    model = analyse_repository(root, scan(root))
    res = generate_for_module(root, model, "calc.ops")
    covered = set(res.functions_covered)
    assert {"calc.ops:clamp", "calc.ops:mean", "calc.ops:discount", "calc.ops:greet"} <= covered
    assert res.functions_skipped["_private"] == "private function"
    assert res.functions_skipped["numbers"] == "generator function"
    # mean([]) raises ZeroDivisionError: reported, not asserted as expected behaviour
    assert any(f.function == "calc.ops:mean" and f.exception == "ZeroDivisionError" for f in res.edge_case_findings)
    assert "pytest.raises(ValueError)" in res.source           # clamp's own validation
    assert "pytest.raises(_mod.InvalidAmount)" in res.source    # module-defined exception
    assert "@given(" in res.source                             # typed functions get property tests
    # the mean property test hits [] -> ZeroDivisionError on the original code, so it is dropped
    dropped = {d["test"] for d in res.dropped_tests}
    assert "test_mean_properties" in dropped
    assert "def test_mean_properties" not in res.source
    assert res.kept_tests == res.generated_tests - len(res.dropped_tests) > 10
    assert res.validation and res.validation["passed"] >= res.kept_tests


def test_kept_tests_catch_a_regression(tmp_path):
    root = write_tree(tmp_path, CALC)
    model = analyse_repository(root, scan(root))
    res = generate_for_module(root, model, "calc.ops")
    test_path = root / res.test_file
    test_path.parent.mkdir(parents=True)
    test_path.write_text(res.source)
    ops = root / "calc" / "ops.py"
    ops.write_text(ops.read_text().replace("max(low, min(value, high))", "max(low, min(value, high - 1))"))
    from codepilot.runner import run_tests
    r = run_tests(root, [res.test_file], with_coverage=False)
    assert r.failed > 0


def test_unimportable_module_is_skipped_cleanly(tmp_path):
    boom = "raise RuntimeError('import side effect')\n\ndef f(x: int):\n    return x\n"
    root = write_tree(tmp_path, {"boom.py": boom})
    model = analyse_repository(root, scan(root))
    res = generate_for_module(root, model, "boom")
    assert res.source == "" and res.functions_skipped["f"].startswith("module could not be imported")


def test_candidate_test_classification(tmp_path):
    root = write_tree(tmp_path, CALC)
    candidate = (
        "from calc.ops import clamp\n"
        "def test_right():\n    assert clamp(5, 0, 3) == 3\n"
        "def test_wrong_assumption():\n    assert clamp(5, 0, 3) == 5\n"
        "def test_hallucinated_api():\n    from calc.ops import clamp_all\n    clamp_all([1])\n")
    out = validate_candidate_tests(root, candidate)
    v = {t["test"]: t["verdict"] for t in out["tests"]}
    assert v == {"test_right": "keep", "test_wrong_assumption": "wrong-assumption",
                 "test_hallucinated_api": "invalid-test"}


def test_classify_failure_and_drop_tests():
    assert classify_failure("error", "fixture missing") == "invalid-test"
    assert classify_failure("failed", "NameError: x") == "invalid-test"
    assert classify_failure("failed", "AssertionError: assert 1 == 2") == "wrong-assumption"
    assert classify_failure("failed", "ZeroDivisionError") == "behaviour-mismatch"
    src = "import x\n\n\ndef test_a():\n    pass\n\n\n@deco\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n"
    out = drop_tests(src, ["test_b"])
    assert "test_b" not in out and "@deco" not in out and "test_a" in out and "test_c" in out
