"""guest.tools — the guest built-in tool definitions and argument validation.

Public contract (docs/GUEST.md):
  ToolSpec(name, side, schema, impl, is_question)   one built-in
  SPECS: dict[name -> ToolSpec]                       every guest built-in
  TOOL_DEFINITIONS: dict[name -> OpenAI tool entry]   {"type": "function",
      "function": {name, description, parameters}} — the shape the request's
      `tools` list takes, so `list(TOOL_DEFINITIONS.values())` is the list.
  validate_args(name, args) -> list[str]              [] == acceptable
  tool_names() -> list[str]                           allowlist.GUEST_BUILTINS order
  impl_for(name) -> callable | None                   (workspace, args, limits) -> str
  path_errors(key, value) -> list[str]                the relative-path rule
  SERIALISED: tools whose ops queue per file (write, edit, apply_patch)

The schemas are written for a 14B model: short, imperative descriptions,
`required` lists and `additionalProperties: false` everywhere. Every `path`
argument is relative to the workspace and must stay inside it after
normalisation (no absolute paths, no `..` escapes). `question` has no
implementation: the coordinator handles it.

Pure: no I/O here; the implementations live in guest.tools.builtin.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any, Callable

from protocol.allowlist import GUEST_BUILTINS

Impl = Callable[..., str]


@dataclass
class ToolSpec:
    name: str
    side: str
    schema: dict
    impl: Impl | None
    is_question: bool = False


# Tools whose operations must run serially per file.
SERIALISED = ("write", "edit", "apply_patch")

# Argument keys that name workspace paths.
PATH_KEYS = ("path",)

TODO_STATES = ("pending", "in_progress", "done")


def _s(description: str, **extra) -> dict:
    return {"type": "string", "description": description, **extra}


def _i(description: str, **extra) -> dict:
    return {"type": "integer", "description": description, **extra}


def _obj(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_PARAMETERS: dict[str, tuple[str, dict]] = {
    "read": (
        "Read a text file from the workspace. Returns numbered lines.",
        _obj({
            "path": _s("Relative file path, e.g. src/app.py."),
            "offset": _i("First line to return (1-based). Default 1.", minimum=1),
            "limit": _i("Maximum number of lines to return. Default 2000.", minimum=1),
        }, ["path"]),
    ),
    "write": (
        "Create or overwrite a file with the given content. Parent folders are created.",
        _obj({
            "path": _s("Relative file path."),
            "content": _s("Full new file content."),
        }, ["path", "content"]),
    ),
    "edit": (
        "Replace one exact text span in a file. old_text must occur exactly once; "
        "include enough surrounding lines to make it unique.",
        _obj({
            "path": _s("Relative file path."),
            "old_text": _s("Exact text to replace. Must match exactly once."),
            "new_text": _s("Replacement text."),
        }, ["path", "old_text", "new_text"]),
    ),
    "apply_patch": (
        "Apply a unified diff (output of `diff -u` / `git diff`) to workspace files. "
        "Use for multi-file or multi-hunk changes.",
        _obj({
            "patch": _s("Unified diff text with --- / +++ headers and @@ hunks."),
        }, ["patch"]),
    ),
    "bash": (
        "Run a shell command in the workspace. Returns stdout and stderr combined. "
        "The command is killed when it exceeds the timeout.",
        _obj({
            "command": _s("Command line to run with bash -c."),
            "timeout_s": {"type": "number", "description":
                          "Seconds before the command is killed. Default: session limit.",
                          "exclusiveMinimum": 0},
        }, ["command"]),
    ),
    "grep": (
        "Search file contents with a regular expression. Returns file:line:text matches.",
        _obj({
            "pattern": _s("Python regular expression to search for."),
            "path": _s("Relative directory or file to search. Default: whole workspace."),
            "glob": _s("Only search files whose name matches this glob, e.g. *.py."),
        }, ["pattern"]),
    ),
    "glob": (
        "List files whose path matches a glob pattern, e.g. **/*.py. Sorted.",
        _obj({
            "pattern": _s("Glob pattern relative to path. ** matches directories."),
            "path": _s("Relative directory to start from. Default: workspace root."),
        }, ["pattern"]),
    ),
    "ls": (
        "List the entries of a directory. Directories end with /.",
        _obj({
            "path": _s("Relative directory. Default: workspace root."),
        }, []),
    ),
    "todo": (
        "Replace the task list with these items. Call again to update states. "
        "Returns the list.",
        _obj({
            "items": {
                "type": "array",
                "description": "The complete list, in order.",
                "items": _obj({
                    "text": _s("What to do, one line."),
                    "state": _s("One of pending, in_progress, done.", enum=list(TODO_STATES)),
                }, ["text", "state"]),
            },
        }, ["items"]),
    ),
    "question": (
        "Ask the user one question and wait for the answer. Use only when you cannot "
        "proceed without it.",
        _obj({
            "text": _s("The question, one or two sentences."),
            "options": {
                "type": "array",
                "description": "Optional short answer choices.",
                "items": {"type": "string"},
            },
        }, ["text"]),
    ),
    "skill": (
        "Load the instructions of a named skill from the workspace.",
        _obj({
            "name": _s("Skill name, e.g. deploy. Loads .agent/skills/<name>/SKILL.md."),
        }, ["name"]),
    ),
}

assert tuple(_PARAMETERS) == GUEST_BUILTINS, "tool schemas must cover GUEST_BUILTINS"

TOOL_DEFINITIONS: dict[str, dict] = {
    name: {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": params},
    }
    for name, (desc, params) in _PARAMETERS.items()
}


def tool_names() -> list[str]:
    """The guest built-ins, in allowlist order."""
    return list(GUEST_BUILTINS)


# ---------------------------------------------------------------- validation

_JSON_TYPES: dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def path_errors(key: str, value: Any) -> list[str]:
    """The workspace path rule: a non-empty relative str that stays inside the
    workspace after normalisation (no absolute paths, drive letters, NUL
    bytes or `..` escapes)."""
    if not isinstance(value, str):
        return [f"{key}: expected str, got {type(value).__name__}"]
    if not value:
        return [f"{key}: must not be empty"]
    if "\0" in value:
        return [f"{key}: must not contain NUL"]
    norm = value.replace("\\", "/")
    if norm.startswith("/") or posixpath.isabs(norm) or (len(norm) > 1 and norm[1] == ":"):
        return [f"{key}: must be relative to the workspace, got {value!r}"]
    normed = posixpath.normpath(norm)
    if normed == ".." or normed.startswith("../"):
        return [f"{key}: escapes the workspace, got {value!r}"]
    return []


def _type_ok(schema: dict, value: Any) -> bool:
    expected = schema.get("type")
    if expected is None:
        return True
    if isinstance(value, bool) and expected in ("integer", "number"):
        return False
    return isinstance(value, _JSON_TYPES[expected])


def _check(schema: dict, value: Any, where: str, out: list[str]) -> None:
    """Minimal JSON-schema checker for the subset the tool schemas use."""
    if not _type_ok(schema, value):
        out.append(f"{where}: expected {schema['type']}, got {type(value).__name__}")
        return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: expected one of {schema['enum']}, got {value!r}")
    if "minimum" in schema and value < schema["minimum"]:
        out.append(f"{where}: must be >= {schema['minimum']}, got {value!r}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        out.append(f"{where}: must be > {schema['exclusiveMinimum']}, got {value!r}")
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}.{key}: missing" if where else f"{key}: missing")
        for key, item in value.items():
            label = f"{where}.{key}" if where else key
            if key not in props:
                if schema.get("additionalProperties", True) is False:
                    out.append(f"{label}: unknown argument")
                continue
            if key in PATH_KEYS and props[key].get("type") == "string":
                out.extend(path_errors(label, item))
                continue
            _check(props[key], item, label, out)
    elif schema.get("type") == "array" and "items" in schema:
        for i, item in enumerate(value):
            _check(schema["items"], item, f"{where}[{i}]", out)


def validate_args(name: str, args: Any) -> list[str]:
    """Errors in `args` for tool `name`: unknown tool, non-dict args, missing
    required keys, wrong types, unknown keys, bad enum values and the path
    rule. [] means the call may be submitted."""
    if name not in SPECS:
        return [f"unknown tool {name!r}"]
    if not isinstance(args, dict):
        return [f"arguments: expected object, got {type(args).__name__}"]
    out: list[str] = []
    _check(SPECS[name].schema, args, "", out)
    if name == "skill" and not out:
        skill_name = args["name"]
        if not skill_name or "/" in skill_name or "\\" in skill_name or skill_name in (".", ".."):
            out.append(f"name: invalid skill name {skill_name!r}")
    return out


def impl_for(name: str) -> Impl | None:
    """The implementation of a guest built-in, or None (unknown or question)."""
    spec = SPECS.get(name)
    return spec.impl if spec else None


# The implementations are imported last: builtin.py is standalone, so the
# package namespace above is complete before it loads.
from guest.tools import builtin as _builtin  # noqa: E402

SPECS: dict[str, ToolSpec] = {
    name: ToolSpec(
        name=name,
        side="guest",
        schema=TOOL_DEFINITIONS[name]["function"]["parameters"],
        impl=_builtin.IMPLS.get(name),
        is_question=(name == "question"),
    )
    for name in GUEST_BUILTINS
}
