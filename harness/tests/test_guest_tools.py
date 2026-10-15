"""guest.tools: schemas, validate_args and the built-in implementations."""
import json
import os
from pathlib import Path

import pytest

from guest import tools
from guest.tools import builtin
from guest.tools import TOOL_DEFINITIONS, SPECS, ToolSpec, path_errors, tool_names, validate_args
from protocol.allowlist import GUEST_BUILTINS

HAPPY = {
    "read": {"path": "src/app.py", "offset": 1, "limit": 10},
    "write": {"path": "notes/out.txt", "content": "hi"},
    "edit": {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
    "apply_patch": {"patch": "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n"},
    "bash": {"command": "ls -la", "timeout_s": 5},
    "grep": {"pattern": "def ", "path": "src", "glob": "*.py"},
    "glob": {"pattern": "**/*.py", "path": "."},
    "ls": {"path": "src"},
    "todo": {"items": [{"text": "write tests", "state": "in_progress"}]},
    "question": {"text": "Which branch?", "options": ["main", "dev"]},
    "skill": {"name": "deploy"},
}

MINIMAL = {
    "read": {"path": "a"}, "write": {"path": "a", "content": ""},
    "edit": {"path": "a", "old_text": "a", "new_text": ""},
    "apply_patch": {"patch": ""}, "bash": {"command": "true"},
    "grep": {"pattern": "x"}, "glob": {"pattern": "*"}, "ls": {},
    "todo": {"items": []}, "question": {"text": "?"}, "skill": {"name": "s"},
}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\n\ndef main():\n    return 1\n")
    (tmp_path / "src" / "util.py").write_text("def helper():\n    return 'main'\n")
    (tmp_path / "README.md").write_text("# Title\nsee main\n")
    return tmp_path


# ------------------------------------------------------------------ schemas

def test_definitions_cover_every_builtin_in_order():
    assert tuple(TOOL_DEFINITIONS) == GUEST_BUILTINS
    assert tool_names() == list(GUEST_BUILTINS)
    assert set(SPECS) == set(GUEST_BUILTINS)


@pytest.mark.parametrize("name", GUEST_BUILTINS)
def test_schema_shape_is_tight_openai_function(name):
    entry = TOOL_DEFINITIONS[name]
    assert entry["type"] == "function"
    fn = entry["function"]
    assert fn["name"] == name
    assert fn["description"].strip() and len(fn["description"]) < 300
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    assert isinstance(params["required"], list)
    for key, prop in params["properties"].items():
        assert prop["description"].strip(), f"{name}.{key} has no description"
    assert set(params["required"]) <= set(params["properties"])
    json.dumps(entry)   # serialisable


@pytest.mark.parametrize("name", GUEST_BUILTINS)
def test_happy_path_and_minimal_args_validate(name):
    assert validate_args(name, HAPPY[name]) == []
    assert validate_args(name, MINIMAL[name]) == []


@pytest.mark.parametrize("name", [n for n in GUEST_BUILTINS if TOOL_DEFINITIONS[n]["function"]["parameters"]["required"]])
def test_missing_required_key_is_reported(name):
    required = TOOL_DEFINITIONS[name]["function"]["parameters"]["required"]
    args = dict(HAPPY[name])
    del args[required[0]]
    errors = validate_args(name, args)
    assert errors and any(required[0] in e and "missing" in e for e in errors)


@pytest.mark.parametrize("name", GUEST_BUILTINS)
def test_unknown_key_is_rejected(name):
    errors = validate_args(name, {**HAPPY[name], "bogus": 1})
    assert errors == ["bogus: unknown argument"]


@pytest.mark.parametrize("name", ["read", "write", "edit", "grep", "glob", "ls"])
@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../b", "..", "", "\\\\srv\\x", "C:\\x"])
def test_escaping_and_absolute_paths_are_rejected(name, bad):
    args = {**HAPPY[name], "path": bad}
    errors = validate_args(name, args)
    assert errors and all(e.startswith("path:") for e in errors), errors


@pytest.mark.parametrize("ok", ["a", "a/b/c.txt", "./a", "a/../b", "a/./b", "..a", "a.."])
def test_inside_paths_pass(ok):
    assert path_errors("path", ok) == []


def test_type_errors_and_enums():
    assert validate_args("read", {"path": "a", "offset": "1"}) == ["offset: expected integer, got str"]
    assert validate_args("read", {"path": "a", "limit": True})[0].startswith("limit: expected integer")
    assert validate_args("read", {"path": "a", "offset": 0}) == ["offset: must be >= 1, got 0"]
    assert validate_args("bash", {"command": "x", "timeout_s": 0}) == ["timeout_s: must be > 0, got 0"]
    assert validate_args("bash", {"command": "x", "timeout_s": 1.5}) == []
    assert validate_args("todo", {"items": [{"text": "t", "state": "later"}]}) == [
        "items[0].state: expected one of ['pending', 'in_progress', 'done'], got 'later'"]
    assert validate_args("todo", {"items": [{"text": "t"}]}) == ["items[0].state: missing"]
    assert validate_args("question", {"text": "q", "options": [1]}) == ["options[0]: expected string, got int"]
    assert validate_args("write", {"path": "a", "content": 3}) == ["content: expected string, got int"]


def test_unknown_tool_non_dict_args_and_skill_names():
    assert validate_args("nope", {}) == ["unknown tool 'nope'"]
    assert validate_args("read", ["a"]) == ["arguments: expected object, got list"]
    assert validate_args("skill", {"name": "../x"}) == ["name: invalid skill name '../x'"]
    assert validate_args("skill", {"name": "a/b"}) == ["name: invalid skill name 'a/b'"]


def test_specs_sides_impls_and_question():
    for name, spec in SPECS.items():
        assert isinstance(spec, ToolSpec) and spec.side == "guest" and spec.name == name
        assert spec.schema is TOOL_DEFINITIONS[name]["function"]["parameters"]
    assert SPECS["question"].is_question and SPECS["question"].impl is None
    assert SPECS["bash"].impl is None            # run by guest.ops
    assert tools.impl_for("question") is None and tools.impl_for("nope") is None
    for name in ("read", "write", "edit", "apply_patch", "grep", "glob", "ls", "todo", "skill"):
        assert callable(SPECS[name].impl) and not SPECS[name].is_question
    assert tools.SERIALISED == ("write", "edit", "apply_patch")


# ------------------------------------------------------------------- read

def test_read_numbers_lines_and_pages(ws):
    out = builtin.read(ws, {"path": "src/app.py"})
    assert out.splitlines() == ["     1\timport os", "     2\t", "     3\tdef main():", "     4\t    return 1"]
    out = builtin.read(ws, {"path": "src/app.py", "offset": 3, "limit": 1})
    assert out.splitlines() == ["     3\tdef main():", "[1 more lines; continue with offset=4]"]
    assert builtin.read(ws, {"path": "src/app.py", "offset": 9}).startswith("Error: offset 9")


def test_read_errors(ws):
    assert builtin.read(ws, {"path": "missing.txt"}) == "Error: file not found: missing.txt"
    assert builtin.read(ws, {"path": "src"}).startswith("Error: src is a directory")
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert builtin.read(ws, {"path": "bin.dat"}) == "Error: bin.dat is a binary file"
    assert builtin.read(ws, {"path": "../etc"}).startswith("Error: path escapes")
    assert builtin.read(ws, {"path": "/etc/hostname"}).startswith("Error: path must be relative")
    (ws / "empty").write_text("")
    assert builtin.read(ws, {"path": "empty"}) == "(empty file)"


def test_read_refuses_symlink_escape(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret"
    outside.write_text("s")
    os.symlink(outside, ws / "link")
    assert builtin.read(ws, {"path": "link"}).startswith("Error: path escapes")


# ------------------------------------------------------------------ write

def test_write_creates_parents_and_overwrites(ws):
    assert builtin.write(ws, {"path": "deep/er/file.txt", "content": "abc"}) == "Wrote 3 bytes to deep/er/file.txt"
    assert (ws / "deep/er/file.txt").read_text() == "abc"
    builtin.write(ws, {"path": "deep/er/file.txt", "content": "xy"})
    assert (ws / "deep/er/file.txt").read_text() == "xy"
    assert builtin.write(ws, {"path": "src", "content": ""}) == "Error: src is a directory"
    assert builtin.write(ws, {"path": "../x", "content": ""}).startswith("Error:")


# ------------------------------------------------------------------- edit

def test_edit_unique_match_rule(ws):
    out = builtin.edit(ws, {"path": "src/app.py", "old_text": "return 1", "new_text": "return 2"})
    assert out.startswith("Edited src/app.py")
    assert "return 2" in (ws / "src/app.py").read_text()
    assert builtin.edit(ws, {"path": "src/app.py", "old_text": "nope", "new_text": ""}) == \
        "Error: old_text not found in src/app.py"
    (ws / "dup.txt").write_text("a\nb\na\n")
    out = builtin.edit(ws, {"path": "dup.txt", "old_text": "a", "new_text": "c"})
    assert out.startswith("Error: old_text matches 2 times in dup.txt")
    assert (ws / "dup.txt").read_text() == "a\nb\na\n"
    out = builtin.edit(ws, {"path": "dup.txt", "old_text": "b\na", "new_text": "b\nc"})
    assert out.startswith("Edited") and (ws / "dup.txt").read_text() == "a\nb\nc\n"
    assert builtin.edit(ws, {"path": "dup.txt", "old_text": "", "new_text": "x"}).startswith("Error:")
    assert builtin.edit(ws, {"path": "missing", "old_text": "a", "new_text": "b"}) == "Error: file not found: missing"


# ------------------------------------------------------------ apply_patch

def test_apply_patch_multi_file_create_modify_delete(ws):
    (ws / "gone.txt").write_text("bye\n")
    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1,4 +1,5 @@\n import os\n+import sys\n \n def main():\n-    return 1\n+    return 2\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    out = builtin.apply_patch(ws, {"patch": patch})
    assert out == "Applied patch to src/app.py, new.txt, gone.txt (deleted)"
    assert (ws / "src/app.py").read_text() == "import os\nimport sys\n\ndef main():\n    return 2\n"
    assert (ws / "new.txt").read_text() == "one\ntwo\n"
    assert not (ws / "gone.txt").exists()


def test_apply_patch_locates_shifted_hunk_and_is_atomic(ws):
    (ws / "f.txt").write_text("pad\npad\na\nb\nc\n")
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    assert builtin.apply_patch(ws, {"patch": patch}).startswith("Applied")
    assert (ws / "f.txt").read_text() == "pad\npad\na\nB\nc\n"
    bad = ("--- a/f.txt\n+++ b/f.txt\n@@ -3,1 +3,1 @@\n-a\n+A\n"
           "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-nope\n+x\n")
    assert builtin.apply_patch(ws, {"patch": bad}) == "Error: README.md: hunk @@ -1 does not apply"
    assert (ws / "f.txt").read_text() == "pad\npad\na\nB\nc\n"      # first file untouched


def test_apply_patch_rejects_malformed_and_escaping(ws):
    assert builtin.apply_patch(ws, {"patch": "just text"}).startswith("Error: malformed patch")
    assert builtin.apply_patch(ws, {"patch": "--- a/x\n+++ b/x\n@@ garbage\n"}).startswith("Error: malformed patch")
    assert builtin.apply_patch(ws, {"patch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n"}).startswith("Error: malformed patch")
    out = builtin.apply_patch(ws, {"patch": "--- /dev/null\n+++ b/../esc\n@@ -0,0 +1 @@\n+x\n"})
    assert out.startswith("Error: path escapes")
    assert builtin.apply_patch(ws, {"patch": "--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-a\n+b\n"}) == \
        "Error: file not found: nope"


# -------------------------------------------------------------- grep/glob/ls

def test_grep_recursive_with_glob_and_caps(ws):
    out = builtin.grep(ws, {"pattern": "main"})
    assert out.splitlines() == ["README.md:2:see main", "src/app.py:3:def main():", "src/util.py:2:    return 'main'"]
    assert builtin.grep(ws, {"pattern": "main", "glob": "*.md"}) == "README.md:2:see main"
    assert builtin.grep(ws, {"pattern": "main", "path": "src/util.py"}) == "src/util.py:2:    return 'main'"
    assert builtin.grep(ws, {"pattern": "zzz"}) == "No matches."
    assert builtin.grep(ws, {"pattern": "("}).startswith("Error: invalid regular expression")
    assert builtin.grep(ws, {"pattern": "x", "path": "nope"}).startswith("Error: path not found")
    (ws / ".harness").mkdir()
    (ws / ".harness" / "todo.json").write_text("main")
    assert ".harness" not in builtin.grep(ws, {"pattern": "main"})
    (ws / "big.txt").write_text("hit\n" * 600)
    out = builtin.grep(ws, {"pattern": "hit", "path": "big.txt"}).splitlines()
    assert len(out) == builtin.GREP_MAX_MATCHES + 1 and out[-1].startswith("[truncated at 500 matches")


def test_glob_sorted_and_scoped(ws):
    assert builtin.glob(ws, {"pattern": "**/*.py"}).splitlines() == ["src/app.py", "src/util.py"]
    assert builtin.glob(ws, {"pattern": "*.py", "path": "src"}).splitlines() == ["src/app.py", "src/util.py"]
    assert builtin.glob(ws, {"pattern": "*"}).splitlines() == ["README.md", "src/"]
    assert builtin.glob(ws, {"pattern": "*.zip"}) == "No files match."
    assert builtin.glob(ws, {"pattern": "/etc/*"}) == "Error: pattern must be relative"
    assert builtin.glob(ws, {"pattern": "*", "path": "nope"}).startswith("Error: directory not found")
    (ws / ".harness").mkdir()
    (ws / ".harness" / "x").write_text("")
    assert ".harness" not in builtin.glob(ws, {"pattern": "**/*"})
    assert "/etc/" not in builtin.glob(ws, {"pattern": "../../../../../../etc/*"})


def test_ls(ws):
    assert builtin.ls(ws, {}).splitlines() == ["README.md", "src/"]
    assert builtin.ls(ws, {"path": "src"}).splitlines() == ["app.py", "util.py"]
    (ws / "empty").mkdir()
    assert builtin.ls(ws, {"path": "empty"}) == "(empty directory)"
    assert builtin.ls(ws, {"path": "README.md"}).startswith("Error: README.md is a file")
    assert builtin.ls(ws, {"path": "nope"}).startswith("Error: path not found")
    (ws / ".harness").mkdir()
    assert ".harness/" not in builtin.ls(ws, {}).splitlines()


# -------------------------------------------------------------- todo/skill

def test_todo_persists_and_echoes(ws):
    items = [{"text": "a", "state": "done"}, {"text": "b", "state": "in_progress"}, {"text": "c", "state": "pending"}]
    assert builtin.todo(ws, {"items": items}) == "[x] a\n[>] b\n[ ] c"
    assert json.loads((ws / ".harness" / "todo.json").read_text()) == items
    assert builtin.todo(ws, {"items": []}) == "Todo list cleared."
    assert json.loads((ws / ".harness" / "todo.json").read_text()) == []
    assert builtin.todo(ws, {"items": [{"text": "x", "state": "nope"}]}).startswith("Error: items[0]")


def test_skill_loads_repo_convention(ws):
    d = ws / ".agent" / "skills" / "deploy"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# Deploy\nrun make deploy\n")
    assert builtin.skill(ws, {"name": "deploy"}) == "# Deploy\nrun make deploy\n"
    assert builtin.skill(ws, {"name": "nope"}) == "Error: skill 'nope' not found (expected .agent/skills/nope/SKILL.md)"
    assert builtin.skill(ws, {"name": "../deploy"}).startswith("Error: invalid skill name")


# ------------------------------------------------------------------- bash

def test_bash_command_argv_and_timeout():
    class L:
        op_timeout_s = {"guest": 42, "host": 1}
    argv, t = builtin.bash_command({"command": "echo hi"}, L())
    assert argv[1:] == ["-c", "echo hi"] and Path(argv[0]).name in ("bash", "sh") and t == 42.0
    assert builtin.bash_command({"command": "x", "timeout_s": 2.5}, L())[1] == 2.5
    assert builtin.bash_command({"command": "x"}, None)[1] == 120.0
