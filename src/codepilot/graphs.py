"""Module 3: call graph and module dependency graph, with change-impact and cycle queries.

Call resolution is static and conservative. A call is linked only when the target can be
found in the repository; everything else (stdlib, third-party, dynamic dispatch) is kept
as an unresolved external name so nothing is silently guessed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import networkx as nx

from .analysis import FunctionInfo, ModuleInfo, RepoModel
from .errors import SymbolNotFoundError


@dataclass
class Graphs:
    calls: nx.DiGraph                     # caller qualname -> callee qualname
    modules: nx.DiGraph                   # importer module -> imported module (repo-internal only)
    external_calls: dict[str, list[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ queries
    def callers(self, qualname: str) -> list[str]:
        self._require(qualname)
        return sorted(self.calls.predecessors(qualname))

    def callees(self, qualname: str) -> list[str]:
        self._require(qualname)
        return sorted(self.calls.successors(qualname))

    def impact(self, qualname: str, max_depth: int | None = None) -> dict:
        """Everything that may break if `qualname` changes: transitive callers, by distance."""
        self._require(qualname)
        seen = {qualname: 0}
        queue = deque([qualname])
        while queue:
            cur = queue.popleft()
            if max_depth is not None and seen[cur] >= max_depth:
                continue
            for caller in self.calls.predecessors(cur):
                if caller not in seen:
                    seen[caller] = seen[cur] + 1
                    queue.append(caller)
        affected = sorted((q for q in seen if q != qualname), key=lambda q: (seen[q], q))
        affected_modules = sorted({q.split(":")[0] for q in affected} | {qualname.split(":")[0]})
        dependent_modules = sorted(
            {m for t in affected_modules if t in self.modules for m in nx.ancestors(self.modules, t)}
            - set(affected_modules))
        return {
            "target": qualname,
            "affected_functions": [{"qualname": q, "distance": seen[q]} for q in affected],
            "affected_modules": affected_modules,
            "modules_importing_affected": dependent_modules,
        }

    def module_cycles(self, limit: int = 50) -> list[list[str]]:
        cycles = []
        for c in nx.simple_cycles(self.modules):
            cycles.append(_rotate(c))
            if len(cycles) >= limit:
                break
        return sorted(cycles)

    def recursive_functions(self) -> list[str]:
        direct = {n for n in self.calls if self.calls.has_edge(n, n)}
        mutual = {n for comp in nx.strongly_connected_components(self.calls) if len(comp) > 1 for n in comp}
        return sorted(direct | mutual)

    def fan_out(self) -> dict[str, int]:
        return {n: self.calls.out_degree(n) for n in self.calls}

    def to_dict(self) -> dict:
        return {
            "call_edges": sorted([list(e) for e in self.calls.edges]),
            "module_edges": sorted([list(e) for e in self.modules.edges]),
            "module_cycles": self.module_cycles(),
            "recursive_functions": self.recursive_functions(),
            "external_calls": self.external_calls,
        }

    def _require(self, qualname: str) -> None:
        if qualname not in self.calls:
            raise SymbolNotFoundError(f"Unknown function: {qualname}")


def _rotate(cycle: list[str]) -> list[str]:
    i = cycle.index(min(cycle))
    return cycle[i:] + cycle[:i]


class _Resolver:
    def __init__(self, model: RepoModel):
        self.model = model
        self.functions = model.functions()
        self.classes = model.classes()

    def _callable(self, module: str, local: str) -> str | None:
        """`local` is a dotted name inside `module`; a class resolves to its __init__."""
        q = f"{module}:{local}"
        if q in self.functions:
            return q
        if q in self.classes and f"{q}.__init__" in self.functions:
            return f"{q}.__init__"
        return None

    def resolve(self, fn: FunctionInfo, mod: ModuleInfo, target: str) -> str | None:
        head, _, rest = target.partition(".")
        local_scope = fn.qualname.split(":")[1]
        # 1. self.method / cls.method inside a class
        if head in ("self", "cls") and fn.parent_class and rest and "." not in rest:
            owner = local_scope.rsplit(".", 1)[0]
            return self._callable(mod.name, f"{owner}.{rest}")
        # 2. nested function visible from the enclosing scope, innermost first
        if not rest:
            scope = local_scope.split(".")
            for i in range(len(scope), 0, -1):
                hit = self._callable(mod.name, ".".join(scope[:i] + [head]))
                if hit:
                    return hit
        # 3. top-level function or class in the same module, or Class.method
        hit = self._callable(mod.name, target)
        if hit:
            return hit
        # 4. names bound by imports
        for imp in mod.imports:
            if imp.alias != head:
                continue
            if imp.name is None:                          # import pkg.mod [as m]; m.f()
                full = imp.module if imp.alias != imp.module.split(".")[0] else imp.module.split(".")[0]
                dotted = f"{full}.{rest}" if rest else full
                mod_part, _, attr = dotted.rpartition(".")
                while mod_part:
                    if mod_part in self.model.modules:
                        return self._callable(mod_part, dotted[len(mod_part) + 1:])
                    mod_part, _, _ = mod_part.rpartition(".")
                return None
            # from pkg import name [as alias]
            if f"{imp.module}.{imp.name}" in self.model.modules and rest:   # imported a module
                return self._callable(f"{imp.module}.{imp.name}", rest)
            if imp.module in self.model.modules:
                return self._callable(imp.module, f"{imp.name}.{rest}" if rest else imp.name)
        return None


def build_graphs(model: RepoModel) -> Graphs:
    calls: nx.DiGraph = nx.DiGraph()
    modules: nx.DiGraph = nx.DiGraph()
    external: dict[str, list[str]] = {}
    resolver = _Resolver(model)

    for name, mod in model.modules.items():
        modules.add_node(name)
        for imp in mod.imports:
            for candidate in (f"{imp.module}.{imp.name}" if imp.name else None, imp.module):
                if candidate and candidate in model.modules and candidate != name:
                    modules.add_edge(name, candidate)
                    break
        for fn in mod.functions:
            calls.add_node(fn.qualname, module=name, lineno=fn.lineno, file=fn.file)

    for mod in model.modules.values():
        for fn in mod.functions:
            unresolved = []
            for t in fn.raw_calls:
                callee = resolver.resolve(fn, mod, t)
                if callee:
                    calls.add_edge(fn.qualname, callee)
                else:
                    unresolved.append(t)
            if unresolved:
                external[fn.qualname] = unresolved
    return Graphs(calls=calls, modules=modules, external_calls=external)
