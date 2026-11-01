import ast
import textwrap

import pytest

from codepilot.modernise import outdated_patterns


def found(src: str) -> list[tuple[str, str]]:
    tree = ast.parse(textwrap.dedent(src))
    return [(f.severity, f.message) for f in outdated_patterns(tree, "pkg.mod", "pkg/mod.py")]


@pytest.mark.parametrize(
    ("src", "severity", "fragment"),
    [
        ("import imp\n", "warning", "removed in Python 3.12; use importlib"),
        ("from distutils.core import setup\n", "warning", "setuptools"),
        ("import pipes\n", "warning", "shlex.quote"),
        ("import optparse\n", "info", "argparse"),
        ("import pkg_resources\n", "warning", "importlib.metadata"),
        ("from collections import Mapping, OrderedDict\n", "warning", "collections.abc"),
        ("from typing import List, Optional\n", "info", "List -> list"),
        ("class A(object):\n    pass\n", "info", "class A:"),
        ("import datetime\nnow = datetime.datetime.utcnow()\n", "warning", "timezone.utc"),
        ("from datetime import datetime\ndatetime.utcnow()\n", "warning", "naive"),
        ("import asyncio\nloop = asyncio.get_event_loop()\n", "warning", "asyncio.run"),
        ("import logging\nlogging.warn('x')\n", "warning", "logging.warning"),
        ("class T:\n    def test(self):\n        self.assertEquals(1, 1)\n", "warning",
         "assertEqual"),
        ("class B(A):\n    def f(self):\n        super(B, self).f()\n", "info", "super()"),
        ("import asyncio\n@asyncio.coroutine\ndef old():\n    yield\n", "warning", "async def"),
    ],
)
def test_outdated_patterns_are_reported_with_replacement(src, severity, fragment):
    results = found(src)
    assert any(sev == severity and fragment in msg for sev, msg in results), results


@pytest.mark.parametrize(
    "src",
    [
        "import importlib\nimport argparse\nfrom collections.abc import Mapping\n",
        "from collections import OrderedDict, defaultdict\n",
        "from typing import Optional, Protocol\n",
        "class A:\n    pass\n",
        "from datetime import datetime, timezone\ndatetime.now(timezone.utc)\n",
        "import asyncio\nasyncio.run(main())\n",
        "class B(A):\n    def f(self):\n        super().f()\n",
        "class T:\n    def test(self):\n        self.assertEqual(1, 1)\n        other.assertEquals(1)\n",
        "from .local import imp\n",
    ],
)
def test_modern_code_is_not_flagged(src):
    assert found(src) == []


def test_findings_carry_symbol_and_line():
    tree = ast.parse("class Job:\n    def run(self):\n        import imp\n")
    [f] = outdated_patterns(tree, "pkg.jobs", "pkg/jobs.py")
    assert (f.rule, f.line, f.symbol) == ("outdated-api", 3, "pkg.jobs:Job.run")
