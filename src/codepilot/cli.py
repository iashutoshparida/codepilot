"""Command-line entry point: `codepilot <command> ...`. Prints JSON unless stated otherwise."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .errors import CodePilotError
from .logging_setup import configure
from .quality import summarise
from .service import CodePilotService


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codepilot", description=__doc__)
    p.add_argument("--log-level", default="WARNING")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("analyse", "graph", "quality"):
        sp = sub.add_parser(name)
        sp.add_argument("repo")
    sp = sub.add_parser("impact")
    sp.add_argument("repo")
    sp.add_argument("qualname", help="module:function, e.g. shop.tax:add_tax")
    sp = sub.add_parser("gen-tests")
    sp.add_argument("repo")
    sp.add_argument("module")
    sp.add_argument("--write", action="store_true", help="write the validated tests into the repository")
    sp = sub.add_parser("run-tests")
    sp.add_argument("repo")
    sp.add_argument("paths", nargs="*", default=["tests"])
    sp = sub.add_parser("mutate")
    sp.add_argument("repo")
    sp.add_argument("--source", nargs="+", required=True)
    sp.add_argument("--tests", nargs="+", default=["tests"])
    sp.add_argument("--max", type=int, default=100)
    sp = sub.add_parser("evaluate")
    sp.add_argument("repo")
    sp.add_argument("--tests", nargs="+", default=["tests"])
    sp.add_argument("--bugs", type=Path)
    sp.add_argument("--max-mutants", type=int)
    sp.add_argument("--markdown", type=Path, help="also write a Markdown report here")
    sp.add_argument("--json", type=Path, help="also write the full JSON result here")
    sp = sub.add_parser("llm-tests", help="LLM-written tests, kept only if safe, correct and stable")
    sp.add_argument("repo")
    sp.add_argument("module")
    sp.add_argument("--runs", type=int, default=3, help="repeat runs used to detect flaky tests")
    sp.add_argument("--write", action="store_true")
    sp = sub.add_parser("pr-tests", help="generate and mutation-test tests for code changed since BASE")
    sp.add_argument("repo")
    sp.add_argument("--base", required=True)
    sp.add_argument("--head", default="HEAD")
    sp.add_argument("--tests", nargs="+", default=["tests"])
    sp.add_argument("--max-mutants", type=int, default=150)
    sp.add_argument("--llm", action="store_true", help="also add LLM tests (CODEPILOT_LLM_PROVIDER)")
    sp.add_argument("--markdown", type=Path)
    sp.add_argument("--write", action="store_true", help="write the kept tests into the repository")
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)
    configure(args.log_level)
    svc = CodePilotService(Settings())
    try:
        if args.cmd == "serve":
            import uvicorn
            uvicorn.run("codepilot.api.app:app", host=args.host, port=args.port)
            return 0
        if args.cmd == "analyse":
            a = svc.analyse(args.repo)
            _print({"inventory": {k: v for k, v in a.inventory.to_dict().items() if k != "files"},
                    "model": a.model.summary(), "quality": summarise(a.findings)["by_rule"],
                    "module_cycles": a.graphs.module_cycles()})
        elif args.cmd == "graph":
            _print(svc.analyse(args.repo).graphs.to_dict())
        elif args.cmd == "quality":
            _print(summarise(svc.analyse(args.repo).findings))
        elif args.cmd == "impact":
            _print(svc.analyse(args.repo).graphs.impact(args.qualname))
        elif args.cmd == "gen-tests":
            from .testgen import generate_for_module
            a = svc.analyse(args.repo)
            res = generate_for_module(a.root, a.model, args.module)
            if args.write and res.source:
                target = a.root / res.test_file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(res.source)
            _print({k: v for k, v in res.to_dict().items() if k != "source"} | {"written": args.write})
        elif args.cmd == "llm-tests":
            from .llm import make_provider
            from .llmtests import generate_llm_tests
            a = svc.analyse(args.repo)
            llm_res = generate_llm_tests(a.root, a.model, args.module,
                                         make_provider(svc.settings), runs=args.runs)
            if args.write and llm_res.kept:
                target = a.root / llm_res.test_file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(llm_res.source)
            _print({k: v for k, v in llm_res.to_dict().items() if k != "source"}
                   | {"written": args.write})
        elif args.cmd == "pr-tests":
            from .llm import make_provider
            from .prtests import analyse_pr_tests
            root = svc.resolve(args.repo)
            pr = analyse_pr_tests(root, args.base, args.tests, args.head,
                                  make_provider(svc.settings) if args.llm else None,
                                  max_mutants=args.max_mutants)
            if args.markdown:
                args.markdown.write_text(pr.to_markdown())
            if args.write:
                for rel, text in pr.test_files.items():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_text(text)
            _print({"changed_files": pr.changed_files, "mutation_before": pr.mutation_before,
                    "mutation_after": pr.mutation_after, "mutants": pr.mutants,
                    "modules": [m.__dict__ for m in pr.modules], "note": pr.note})
        elif args.cmd == "run-tests":
            from .runner import run_tests
            _print(run_tests(svc.resolve(args.repo), args.paths).to_dict())
        elif args.cmd == "mutate":
            from .mutation import run_mutation
            _print(run_mutation(svc.resolve(args.repo), args.source, args.tests, max_mutants=args.max).to_dict())
        elif args.cmd == "evaluate":
            from .evaluate import evaluate, to_markdown
            r = evaluate(svc.resolve(args.repo), args.tests, args.bugs, args.max_mutants)
            if args.markdown:
                args.markdown.write_text(to_markdown(r))
            if args.json:
                args.json.write_text(json.dumps({k: v for k, v in r.items() if k != "generated_files"}, indent=2))
            _print({k: r[k] for k in ("baseline", "with_generated", "generation")} | {"seconds": r["seconds"]})
    except CodePilotError as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
