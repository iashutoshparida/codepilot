"""Module 6: generate pytest tests from observed behaviour, then validate them.

The generator never guesses expected values. It:
1. builds inputs per parameter (typical, boundary, empty and invalid values) from type
   hints, defaults and parameter names;
2. runs the real function on each input in a sandboxed subprocess and records the
   return value or exception;
3. writes characterisation tests that pin that behaviour (useful as a regression net
   before refactoring) and Hypothesis property tests for typed functions;
4. reports crash-type exceptions (ZeroDivisionError, IndexError, ...) as findings for a
   human to review instead of locking them in as "expected";
5. runs every generated test against the unmodified code and drops any that do not pass.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analysis import FunctionInfo, RepoModel
from .runner import RunReport, run_tests_in
from .sandbox import import_roots, scrubbed_env, workspace

log = logging.getLogger("codepilot.testgen")

POOLS: dict[str, list] = {
    "int": [3, 0, 1, -1, 100],
    "float": [2.5, 0.0, -1.0, 1000000.0],
    "str": ["abc", "", " ", "Ünïcode"],
    "bool": [True, False],
    "list_num": [[1, 2, 3], [], [0], [-5, 10]],
    "list_str": [["a", "b"], [], [""]],
    "dict": [{"a": 1}, {}],
    "any": [1, "a", None, []],
}
NAME_HINTS = [
    (re.compile(r"^(n|i|j|k|count|qty|quantity|size|index|idx|num|limit|times|level|days|age)$|_count$|_qty$"),
     "int"),
    (re.compile(r"price|amount|rate|ratio|cost|total|balance|weight|score|percent|discount|fee"), "float"),
    (re.compile(r"^(name|text|s|msg|message|label|code|sku|key|path|word|title|email|query|prefix)$|_name$|_id$"),
     "str"),
    (re.compile(r"^(items|values|xs|nums|numbers|prices|amounts|scores|data|seq|lst|orders)$"), "list_num"),
    (re.compile(r"^(names|words|tags|lines|skus|keys)$"), "list_str"),
    (re.compile(r"^(is_|has_|allow|enable|flag|strict|verbose)"), "bool"),
]
SUSPICIOUS = {"ZeroDivisionError", "IndexError", "KeyError", "AttributeError", "UnboundLocalError",
              "RecursionError", "NameError", "OverflowError"}
VALIDATION = {"ValueError", "TypeError"}
MAX_CASES = 15

PROBE_HARNESS = textwrap.dedent('''
    import ast, importlib, json, signal, sys

    def _alarm(*_):
        raise TimeoutError("case timed out")

    signal.signal(signal.SIGALRM, _alarm)
    spec = json.loads(sys.stdin.read())
    mod = importlib.import_module(spec["module"])
    out = {}
    for fname, cases in spec["functions"].items():
        fn = getattr(mod, fname)
        results = []
        for args in cases:
            runs = []
            for _ in range(2):
                signal.alarm(2)
                try:
                    v = fn(*ast.literal_eval(args))
                    r = repr(v)
                    try:
                        literal = ast.literal_eval(r) == v
                    except Exception:
                        literal = False
                    runs.append({"ok": True, "repr": r, "literal": literal, "float": isinstance(v, float)})
                except BaseException as e:  # noqa: BLE001 - we record whatever the code raises
                    cls = type(e)
                    runs.append({"ok": False, "exc": cls.__name__,
                                 "exc_module": cls.__module__, "msg": str(e)[:200]})
                finally:
                    signal.alarm(0)
            results.append({"args": args, "first": runs[0], "deterministic": runs[0] == runs[1]})
        out[fname] = results
    print("__PROBE__" + json.dumps(out))
''')


@dataclass
class EdgeCaseFinding:
    function: str
    args: str
    exception: str
    message: str


@dataclass
class GenerationResult:
    module: str
    test_file: str
    source: str
    functions_covered: list[str] = field(default_factory=list)
    functions_skipped: dict[str, str] = field(default_factory=dict)
    generated_tests: int = 0
    kept_tests: int = 0
    dropped_tests: list[dict] = field(default_factory=list)
    edge_case_findings: list[EdgeCaseFinding] = field(default_factory=list)
    validation: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edge_case_findings"] = [asdict(f) for f in self.edge_case_findings]
        return d


# --------------------------------------------------------------------------- inputs
def _kind_from_annotation(ann: str | None) -> str | None:
    if not ann:
        return None
    a = ann.replace("typing.", "").replace(" ", "")
    nullable = "None" in a or a.startswith("Optional[")
    a = re.sub(r"^Optional\[(.*)\]$", r"\1", a).replace("|None", "").replace("None|", "")
    base = {"int": "int", "float": "float", "str": "str", "bool": "bool", "dict": "dict"}.get(a)
    if base is None and re.match(r"^(list|List|Sequence|Iterable|tuple)", a):
        base = "list_str" if "str" in a else "list_num"
    if base is None and re.match(r"^(dict|Dict|Mapping)\[", a):
        base = "dict"
    if base and nullable:
        return base + "?"
    return base


def _kind_from_name(name: str) -> str | None:
    for pattern, kind in NAME_HINTS:
        if pattern.search(name):
            return kind
    return None


_LITERAL_KINDS = {int: "int", float: "float", str: "str", bool: "bool", list: "list_num", dict: "dict"}


def _pool_for(p) -> list:
    kind = _kind_from_annotation(p.annotation)
    if kind is None and p.default not in (None, "None"):
        try:
            dv = ast.literal_eval(p.default)
            kind = _LITERAL_KINDS.get(type(dv))
        except Exception:
            kind = None
    kind = kind or _kind_from_name(p.name) or "any"
    nullable = kind.endswith("?")
    pool = list(POOLS[kind.rstrip("?")])
    if nullable:
        pool.append(None)
    return pool


def build_cases(fn: FunctionInfo) -> list[str] | None:
    """One-factor-at-a-time: a typical call, then vary one argument across its pool."""
    params = [p for p in fn.params if p.kind == "positional"]
    if any(p.kind in ("vararg", "kwarg") for p in fn.params) and not params:
        return None
    required = [p for p in params if not p.has_default]
    pools = [_pool_for(p) for p in required]
    base = [pool[0] for pool in pools]
    cases: list[tuple] = [tuple(base)]
    for i, pool in enumerate(pools):
        for v in pool[1:]:
            varied = list(base)
            varied[i] = v
            cases.append(tuple(varied))
    seen, out = set(), []
    for c in cases:
        r = repr(c)
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:MAX_CASES]


def _skip_reason(fn: FunctionInfo, source: str) -> str | None:
    if fn.is_method or "." in fn.qualname.split(":")[1]:
        return "methods and nested functions are not generated in v1"
    if fn.is_async:
        return "async function"
    if fn.name.startswith("_"):
        return "private function"
    body = "\n".join(source.splitlines()[fn.lineno - 1: fn.end_lineno])
    if re.search(r"\byield\b", body):
        return "generator function"
    if any(d not in ("staticmethod",) for d in fn.decorators):
        return "decorated function"
    return None


# --------------------------------------------------------------------------- probing
def probe(ws: Path, module: str, cases: dict[str, list[str]], timeout_s: float = 60) -> dict:
    harness = ws / ".codepilot_probe.py"
    harness.write_text(PROBE_HARNESS)
    spec = json.dumps({"module": module, "functions": cases})
    try:
        p = subprocess.run([sys.executable, str(harness)], cwd=ws, input=spec, capture_output=True, text=True,
                           timeout=timeout_s, env=scrubbed_env(import_roots(ws)))
    except subprocess.TimeoutExpired:
        return {}
    marker = p.stdout.rfind("__PROBE__")
    if marker < 0:
        log.warning("probe failed", extra={"target_module": module, "stderr": p.stderr[-500:]})
        return {}
    return json.loads(p.stdout[marker + len("__PROBE__"):])


# --------------------------------------------------------------------------- rendering
def _safe_ident(s: str) -> str:
    return re.sub(r"\W+", "_", s).strip("_").lower()[:40] or "case"


def _render(module: str, probes: dict, fns: dict[str, FunctionInfo], module_excs: set[str],
            result: GenerationResult) -> str:
    lines = [
        f'"""Tests generated by CodePilot from observed behaviour of `{module}`.',
        "",
        "Characterisation tests pin current behaviour so refactors can be checked for regressions.",
        "Review before relying on them: they assert what the code does, not what it should do.",
        '"""',
        "import pytest",
    ]
    prop_needed = False
    body: list[str] = []
    imported = sorted(probes)
    lines.append(f"from {module} import {', '.join(imported)}")
    if module_excs:
        lines.append(f"import {module} as _mod")
    for fname in imported:
        n = 0
        allowed_excs: set[str] = set()
        for case in probes[fname]:
            args, first = case["args"], case["first"]
            call = f"{fname}(*{args})"
            if not case["deterministic"]:
                result.functions_skipped[fname] = "non-deterministic output"
                break
            n += 1
            name = f"test_{fname}_{n}"
            if first["ok"]:
                if not first["literal"]:
                    n -= 1
                    continue
                expected = first["repr"]
                if first["float"]:
                    expected = f"pytest.approx({expected})"
                body += ["", "", f"def {name}():", f"    assert {call} == {expected}"]
            else:
                exc = first["exc"]
                if exc in SUSPICIOUS or exc == "TimeoutError":
                    result.edge_case_findings.append(EdgeCaseFinding(f"{module}:{fname}", args, exc, first["msg"]))
                    n -= 1
                    continue
                if first["exc_module"] == "builtins":
                    ref = exc
                elif first["exc_module"] == module and exc in module_excs:
                    ref = f"_mod.{exc}"
                else:
                    n -= 1
                    continue
                if exc in VALIDATION or ref.startswith("_mod."):
                    allowed_excs.add(ref)
                body += ["", "", f"def {name}():", f"    with pytest.raises({ref}):", f"        {call}"]
            result.generated_tests += 1
        prop = _property_test(fns[fname], allowed_excs)
        if prop:
            prop_needed = True
            body += ["", ""] + prop
            result.generated_tests += 1
        if n or prop:
            result.functions_covered.append(f"{module}:{fname}")
    if prop_needed:
        lines.append("from hypothesis import given, settings, strategies as st")
    return "\n".join(lines + body) + "\n"


STRATEGIES = {
    "int": "st.integers(min_value=-1000, max_value=1000)",
    "float": "st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)",
    "str": "st.text(max_size=20)",
    "bool": "st.booleans()",
    "list_num": "st.lists(st.integers(min_value=-100, max_value=100), max_size=10)",
    "list_str": "st.lists(st.text(max_size=5), max_size=5)",
}


def _property_test(fn: FunctionInfo, allowed: set[str]) -> list[str] | None:
    required = [p for p in fn.params if p.kind == "positional" and not p.has_default]
    if not required:
        return None
    strats = []
    for p in required:
        kind = _kind_from_annotation(p.annotation)
        if not kind or kind.endswith("?") or kind not in STRATEGIES:
            return None  # only fully typed functions get property tests
        strats.append(f"{p.name}={STRATEGIES[kind]}")
    names = ", ".join(p.name for p in required)
    catch = f"({', '.join(sorted(allowed))},)" if allowed else None
    out = ["@settings(max_examples=60, deadline=None)", f"@given({', '.join(strats)})",
           f"def test_{fn.name}_properties({names}):",
           '    """Runs on generated inputs: no unexpected exception, and the same input gives the same output."""']
    if catch:
        out += ["    try:", f"        first = {fn.name}({names})", f"    except {catch}:", "        return",
                f"    assert {fn.name}({names}) == first"]
    else:
        out += [f"    first = {fn.name}({names})", f"    assert {fn.name}({names}) == first"]
    return out


# --------------------------------------------------------------------------- public API
def generate_for_module(repo: Path, model: RepoModel, module: str, validate: bool = True,
                        timeout_s: float = 120) -> GenerationResult:
    mod = model.modules[module]
    source = (repo / mod.file).read_text(encoding="utf-8", errors="replace")
    test_file = f"tests/codepilot_generated/test_gen_{_safe_ident(module)}.py"
    result = GenerationResult(module=module, test_file=test_file, source="")
    fns, cases = {}, {}
    for fn in mod.functions:
        reason = _skip_reason(fn, source)
        built = None if reason else build_cases(fn)
        if reason or not built:
            result.functions_skipped[fn.name] = reason or "no callable inputs could be built"
            continue
        fns[fn.name] = fn
        cases[fn.name] = built
    module_excs = {c.name for c in mod.classes if any("Error" in b or "Exception" in b for b in c.bases)}
    with workspace(repo) as ws:
        probes = probe(ws, module, cases, timeout_s) if cases else {}
        if cases and not probes:
            result.functions_skipped.update({f: "module could not be imported for probing" for f in cases})
            return result
        result.source = _render(module, probes, fns, module_excs, result) if probes else ""
        if validate and result.source:
            report = validate_tests_in(ws, test_file, result.source, timeout_s)
            result.validation = report.to_dict()
            failing = [o for o in report.outcomes if o.outcome != "passed"]
            if failing:
                result.source = drop_tests(result.source, [o.nodeid.split("::")[-1] for o in failing])
                result.dropped_tests = [{"test": o.nodeid.split("::")[-1], "outcome": o.outcome,
                                         "reason": classify_failure(o.outcome, o.message), "message": o.message[:200]}
                                        for o in failing]
            result.kept_tests = result.generated_tests - len(failing)
        else:
            result.kept_tests = result.generated_tests
    log.info("generated tests", extra={"target_module": module, "kept": result.kept_tests,
                                       "findings": len(result.edge_case_findings)})
    return result


def validate_tests_in(ws: Path, rel_path: str, source: str, timeout_s: float = 120) -> RunReport:
    p = ws / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source)
    return run_tests_in(ws, [rel_path], timeout_s, with_coverage=False)


def validate_candidate_tests(repo: Path, source: str, rel_path: str = "tests/test_candidate.py",
                             timeout_s: float = 120) -> dict:
    """Check tests from any source (an LLM, a person) against the unmodified code."""
    with workspace(repo) as ws:
        report = validate_tests_in(ws, rel_path, source, timeout_s)
    verdicts = [{"test": o.nodeid.split("::")[-1], "outcome": o.outcome,
                 "verdict": "keep" if o.outcome == "passed" else classify_failure(o.outcome, o.message),
                 "message": o.message[:300]} for o in report.outcomes]
    return {"summary": {"total": len(verdicts), "keep": sum(v["verdict"] == "keep" for v in verdicts)},
            "tests": verdicts}


def classify_failure(outcome: str, message: str) -> str:
    """Why a generated test did not pass on the original code."""
    if outcome == "error":
        return "invalid-test"           # fixture/collection/setup problem: the test itself is broken
    if re.search(r"\b(NameError|ImportError|ModuleNotFoundError|SyntaxError|AttributeError)\b", message):
        return "invalid-test"           # refers to things that do not exist
    if "AssertionError" in message or message.startswith("assert"):
        return "wrong-assumption"       # expected value disagrees with the code: needs a human
    return "behaviour-mismatch"         # raised something unexpected


def drop_tests(source: str, names: list[str]) -> str:
    """Remove top-level test functions (and their decorators) by name."""
    if not names:
        return source
    out, skip, pending = [], False, []
    for line in source.splitlines():
        if line.startswith("@"):
            pending.append(line)
            continue
        m = re.match(r"def (test_\w+)\(", line)
        if m:
            skip = m.group(1) in names
            if not skip:
                out += pending
            pending = []
        elif line and not line.startswith((" ", "\t")):
            skip = False
            out += pending
            pending = []
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"
