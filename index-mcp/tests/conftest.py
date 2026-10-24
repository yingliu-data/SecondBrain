"""A small Python repo, written into `tmp_path` at test time: a checked-in
file with a syntax error would fail the lint job, and checked-in `test_*.py`
fixtures would be collected by pytest."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import mongomock
import pytest

from index_mcp.store import Store

NODE_FIELDS = {"_id", "nid", "type", "source", "path", "title", "heading_path", "names", "start_line", "end_line",
               "text", "text_hash", "version", "is_latest", "created_at", "superseded_at", "superseded_by", "removed_at"}

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
        # shapes\x0c page two
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
    "venv/installed.py": "def installed():\n    pass\n",
    "venv/pyvenv.cfg": "",
    "env/settings.py": "DEBUG = False\n",
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


class FakeStore(Store):
    """`Store` on mongomock, which has no Atlas Search: `keyword_names` matches a word of
    the names as `lucene.simple` does, the two chunk searches become substring matches,
    and `vector` raises `vector_error` when a test sets it."""

    vector_error: Exception | None = None

    def ensure_search_indexes(self):
        pass

    def keyword_names(self, query, match, limit=10):
        words = set(_words(query))
        found = [doc for doc in self.nodes.find(match) if words & set(_words(" ".join([*doc["names"], *doc["heading_path"], doc["stem"]])))]
        return found[:limit]

    def keyword_text(self, query, match, limit=10):
        return self._substring(self.chunks, query, lambda doc: [doc["text"]], match, limit)

    def vector(self, query, match, limit=10):
        if self.vector_error:
            raise self.vector_error
        return self.keyword_text(query, match, limit)

    def _substring(self, collection, query, texts_of, match, limit):
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        found = [doc for doc in collection.find(match) if any(pattern.search(t) for t in texts_of(doc))]
        return found[:limit]


def _words(text: str) -> list[str]:
    """The tokens of `lucene.simple`: runs of letters, lower-cased."""
    return re.findall(r"[^\W\d_]+", text.lower())


@pytest.fixture
def store() -> FakeStore:
    return FakeStore(mongomock.MongoClient(tz_aware=True), "test")
