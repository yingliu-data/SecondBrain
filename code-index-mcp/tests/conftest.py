"""A small Python repo, written into `tmp_path` at test time: a checked-in
file with a syntax error would fail the lint job, and checked-in `test_*.py`
fixtures would be collected by pytest."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

FILES = {
    "main.py": """
        import os
        import pkg.utils
        import pkg.shapes as shapes
        from pkg import sub
        from pkg.utils import calculate_sum
        from pkg.sub.tools import LIMIT
        from pkg.sub.helpers import *
        from outside import thing


        def local():
            return LIMIT


        def bare_local():
            local()
            local()


        def bare_imported():
            return calculate_sum(1, 2)


        def module_attr():
            return shapes.Shape()


        def no_edges(obj):
            obj.method()
            obj.get()()
            getattr(obj, "name")()
            missing()
            os.getcwd()
            pkg.utils.calculate_sum(1, 2)
            total([1])
            thing()


        class Square(shapes.Shape):
            pass
    """,
    "pkg/__init__.py": "",
    "pkg/utils.py": """
        CONSTANT = 1


        def calculate_sum(a, b):
            return a + b


        class MathUtils:
            def calculate_sum(self, a, b):
                return calculate_sum(a, b)

            def double(self, a):
                return self.calculate_sum(a, a)

            @classmethod
            def make(cls):
                return cls.create()

            @classmethod
            def create(cls):
                return cls()


        class Base:
            pass


        class Child(Base):
            pass
    """,
    "pkg/shapes.py": """
        class Shape:
            pass
    """,
    "pkg/sub/__init__.py": "",
    "pkg/sub/tools.py": """
        LIMIT = 10


        def calculate_sum(xs):
            return sum(xs)
    """,
    "pkg/sub/helpers.py": """
        from . import tools
        from ..utils import MathUtils
        from .... import too_far


        def total(xs):
            return tools.calculate_sum(xs)


        class Fancy(MathUtils):
            pass
    """,
    "tests/test_x.py": """
        def only_in_tests():
            pass
    """,
    "foo_test.py": """
        def only_in_foo_test():
            pass
    """,
    "broken.py": "def oops(:\n",
    ".venv/hidden.py": "def hidden():\n    pass\n",
}


def write_repo(root: Path) -> Path:
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
    (root / "binary.py").write_bytes(b"x = '\xff'\n")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return write_repo(tmp_path / "repo")
