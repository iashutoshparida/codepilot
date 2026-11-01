"""Application service: one place that wires the modules together for the API and CLI."""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .analysis import RepoModel, analyse_repository
from .assistant import CodeAssistant
from .config import Settings
from .graphs import Graphs, build_graphs
from .ingestion import Inventory, scan
from .llm import LLMProvider, make_provider
from .quality import Finding, analyse_quality
from .refactor import RefactorWorkflow

log = logging.getLogger("codepilot.service")


@dataclass
class Analysis:
    root: Path
    inventory: Inventory
    model: RepoModel
    graphs: Graphs
    findings: list[Finding]


def _fingerprint(root: Path, inv: Inventory) -> str:
    h = hashlib.sha1()
    for f in inv.files:
        st = (root / f.path).stat()
        h.update(f"{f.path}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()


class CodePilotService:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None, state_root: Path | None = None):
        self.settings = settings
        self._provider = provider
        self.state_root = state_root or Path(os.environ.get("CODEPILOT_STATE_DIR", "~/.codepilot")).expanduser()
        self._cache: dict[str, tuple[str, Analysis]] = {}

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = make_provider(self.settings)
        return self._provider

    def resolve(self, repo_path: str) -> Path:
        return self.settings.resolve_repo(repo_path)

    def analyse(self, repo_path: str) -> Analysis:
        """Analyse a repository; results are reused until any tracked file changes."""
        root = self.resolve(repo_path)
        inv = scan(root)
        key, fp = str(root), _fingerprint(root, inv)
        cached = self._cache.get(key)
        if cached and cached[0] == fp:
            return cached[1]
        model = analyse_repository(root, inv)
        graphs = build_graphs(model)
        result = Analysis(root, inv, model, graphs, analyse_quality(root, model, graphs))
        self._cache[key] = (fp, result)
        return result

    def assistant(self, repo_path: str) -> CodeAssistant:
        a = self.analyse(repo_path)
        return CodeAssistant(a.root, a.model, a.graphs, self.provider, a.findings)

    def workflow(self, repo_path: str, test_paths: list[str]) -> RefactorWorkflow:
        root = self.resolve(repo_path)
        state = self.state_root / hashlib.sha1(str(root).encode()).hexdigest()[:16]
        return RefactorWorkflow(root, state, test_paths, self.settings.test_timeout_s)
