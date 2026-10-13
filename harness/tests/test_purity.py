"""The protocol, guest and bench packages import the standard library only
(bench may also use the stdlib test doubles in tests/fakes)."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"protocol", "guest"}
ALSO = {"bench": {"tests"}}     # package -> extra local packages it may import


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_stdlib_only():
    bad = []
    for pkg in sorted(LOCAL | set(ALSO)):
        allowed = LOCAL | {pkg} | ALSO.get(pkg, set())
        for py in (ROOT / pkg).rglob("*.py"):
            for mod in _imports(py):
                if mod in allowed or mod in sys.stdlib_module_names:
                    continue
                bad.append(f"{py.relative_to(ROOT)}: {mod}")
    assert not bad, "third-party imports:\n" + "\n".join(bad)
