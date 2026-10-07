"""The protocol and guest packages import the standard library only."""
import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {"protocol", "guest"}


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
    for pkg in LOCAL:
        for py in (ROOT / pkg).rglob("*.py"):
            for mod in _imports(py):
                if mod in LOCAL or mod in sys.stdlib_module_names:
                    continue
                bad.append(f"{py.relative_to(ROOT)}: {mod}")
    assert not bad, "third-party imports:\n" + "\n".join(bad)
