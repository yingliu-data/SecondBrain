"""guest.tools.builtin — implementations of the guest built-in tools.

Each implementation is a plain function `(workspace: Path, args: dict, limits)
-> str` returning the result text. Errors are returned as "Error: ..."
strings; only programming errors raise. Arguments are assumed to have passed
`guest.tools.validate_args`, but every path is resolved again here and
rejected when it leaves the workspace (symlink escapes included).

`bash` is executed by guest.ops (process group, timeout, kill); this module
only exposes `bash_command(args, limits) -> (argv, timeout_s)`.

Caps: read 2000 lines / 2000 chars per line, grep 500 matches, glob and ls
2000 entries. Longer results are still truncated by the operation manager's
spill rule; these caps keep single tool outputs sane before that.

IMPLS: dict[name -> impl] is what guest.tools binds into its ToolSpecs.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from pathlib import Path

READ_DEFAULT_LIMIT = 2000
READ_MAX_LINE_CHARS = 2000
GREP_MAX_MATCHES = 500
LIST_MAX_ENTRIES = 2000
HARNESS_DIR = ".harness"           # excluded from grep/glob/ls listings
SKIP_DIRS = (HARNESS_DIR, ".git")
TODO_STATES = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}


class PathError(Exception):
    """Raised by resolve_path; the implementations turn it into an Error string."""


def resolve_path(workspace: Path, rel: str) -> Path:
    """`workspace / rel`, resolved, provided it stays inside the workspace.

    Raises PathError on absolute paths, `..` escapes and symlinks that point
    outside the workspace."""
    if not isinstance(rel, str) or not rel or "\0" in rel:
        raise PathError(f"invalid path {rel!r}")
    if Path(rel).is_absolute():
        raise PathError(f"path must be relative to the workspace: {rel!r}")
    root = workspace.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise PathError(f"path escapes the workspace: {rel!r}")
    return target


def _rel(workspace: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _read_text(p: Path) -> str:
    """UTF-8 text of a file; raises PathError for binary content."""
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise PathError("binary file")
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------- read

def read(workspace: Path, args: dict, limits=None) -> str:
    try:
        p = resolve_path(workspace, args["path"])
    except PathError as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: file not found: {args['path']}"
    if p.is_dir():
        return f"Error: {args['path']} is a directory; use ls"
    try:
        text = _read_text(p)
    except PathError:
        return f"Error: {args['path']} is a binary file"
    except OSError as e:
        return f"Error: cannot read {args['path']}: {e.strerror or e}"
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or READ_DEFAULT_LIMIT)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        return "(empty file)"
    if offset > total:
        return f"Error: offset {offset} is past the end of the file ({total} lines)"
    chunk = lines[offset - 1: offset - 1 + limit]
    out = []
    for i, line in enumerate(chunk, start=offset):
        if len(line) > READ_MAX_LINE_CHARS:
            line = line[:READ_MAX_LINE_CHARS] + " [line truncated]"
        out.append(f"{i:6d}\t{line}")
    last = offset - 1 + len(chunk)
    if last < total:
        out.append(f"[{total - last} more lines; continue with offset={last + 1}]")
    return "\n".join(out)


# --------------------------------------------------------------------- write

def write(workspace: Path, args: dict, limits=None) -> str:
    try:
        p = resolve_path(workspace, args["path"])
    except PathError as e:
        return f"Error: {e}"
    if p.is_dir():
        return f"Error: {args['path']} is a directory"
    content = args["content"]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        p.write_bytes(data)
    except OSError as e:
        return f"Error: cannot write {args['path']}: {e.strerror or e}"
    return f"Wrote {len(data)} bytes to {_rel(workspace, p)}"


# ---------------------------------------------------------------------- edit

def edit(workspace: Path, args: dict, limits=None) -> str:
    try:
        p = resolve_path(workspace, args["path"])
    except PathError as e:
        return f"Error: {e}"
    if not p.is_file():
        return f"Error: file not found: {args['path']}"
    old, new = args["old_text"], args["new_text"]
    if old == "":
        return "Error: old_text must not be empty"
    try:
        text = _read_text(p)
    except PathError:
        return f"Error: {args['path']} is a binary file"
    except OSError as e:
        return f"Error: cannot read {args['path']}: {e.strerror or e}"
    n = text.count(old)
    if n == 0:
        return f"Error: old_text not found in {args['path']}"
    if n > 1:
        return (f"Error: old_text matches {n} times in {args['path']}; "
                f"include more surrounding text to make it unique")
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
    except OSError as e:
        return f"Error: cannot write {args['path']}: {e.strerror or e}"
    return (f"Edited {_rel(workspace, p)}: replaced {old.count(chr(10)) + 1} line(s) "
            f"with {new.count(chr(10)) + 1}")


# --------------------------------------------------------------- apply_patch

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(name: str) -> str:
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    name = name.split("\t")[0]
    if name == "/dev/null":
        return name
    for prefix in ("a/", "b/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def parse_patch(text: str) -> list[dict]:
    """Parse a unified diff into [{old, new, hunks: [(old_start, lines)]}].

    `lines` are the raw hunk lines (' ', '-', '+', '\\'). Raises ValueError
    with a line number on malformed input."""
    files: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise ValueError(f"line {i + 2}: expected '+++' after '---'")
            entry = {"old": _strip_prefix(line[4:]), "new": _strip_prefix(lines[i + 1][4:]),
                     "hunks": []}
            files.append(entry)
            i += 2
            while i < len(lines) and lines[i].startswith("@@"):
                m = _HUNK_RE.match(lines[i])
                if not m:
                    raise ValueError(f"line {i + 1}: malformed hunk header {lines[i]!r}")
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                i += 1
                body: list[str] = []
                seen_old = seen_new = 0
                while i < len(lines) and (seen_old < old_count or seen_new < new_count):
                    hl = lines[i]
                    if hl.startswith("\\"):
                        body.append(hl)
                    elif hl.startswith(" ") or hl == "":
                        body.append(" " + hl[1:])
                        seen_old += 1
                        seen_new += 1
                    elif hl.startswith("-"):
                        body.append(hl)
                        seen_old += 1
                    elif hl.startswith("+"):
                        body.append(hl)
                        seen_new += 1
                    else:
                        raise ValueError(f"line {i + 1}: unexpected hunk line {hl!r}")
                    i += 1
                if seen_old != old_count or seen_new != new_count:
                    raise ValueError(f"hunk at line {old_start}: line counts do not match header")
                while i < len(lines) and lines[i].startswith("\\"):
                    body.append(lines[i])
                    i += 1
                entry["hunks"].append((old_start, body))
            if not entry["hunks"] and entry["new"] != "/dev/null" and entry["old"] != "/dev/null":
                raise ValueError(f"no hunks for {entry['new']}")
        else:
            i += 1   # diff --git / index / mode lines and other noise
    if not files:
        raise ValueError("no file headers (--- / +++) found")
    return files


def _apply_hunks(old_lines: list[str], hunks: list[tuple[int, list[str]]]) -> list[str]:
    """Apply hunks in order; each is located at its header position first,
    then by searching for its old side nearby. Raises ValueError."""
    result = list(old_lines)
    offset = 0
    for old_start, body in hunks:
        old_side = [h[1:] for h in body if h[0] in " -"]
        new_side = [h[1:] for h in body if h[0] in " +"]
        pos = _locate(result, old_side, old_start - 1 + offset)
        if pos is None:
            raise ValueError(f"hunk @@ -{old_start} does not apply")
        result[pos:pos + len(old_side)] = new_side
        offset += len(new_side) - len(old_side)
    return result


def _locate(lines: list[str], old_side: list[str], guess: int) -> int | None:
    if not old_side:
        return min(max(guess, 0), len(lines))
    n = len(old_side)
    candidates = [guess] + [guess + d for k in range(1, len(lines) + 1) for d in (-k, k)]
    seen = set()
    for pos in candidates:
        if pos < 0 or pos + n > len(lines) or pos in seen:
            continue
        seen.add(pos)
        if lines[pos:pos + n] == old_side:
            return pos
    return None


def apply_patch(workspace: Path, args: dict, limits=None) -> str:
    try:
        files = parse_patch(args["patch"])
    except ValueError as e:
        return f"Error: malformed patch: {e}"
    plan: list[tuple[Path, str | None, str]] = []   # (target, new_text|None=delete, label)
    for entry in files:
        rel = entry["new"] if entry["new"] != "/dev/null" else entry["old"]
        try:
            target = resolve_path(workspace, rel)
        except PathError as e:
            return f"Error: {e}"
        creating = entry["old"] == "/dev/null"
        deleting = entry["new"] == "/dev/null"
        if creating:
            if target.exists():
                return f"Error: {rel} already exists (patch creates it)"
            old_lines: list[str] = []
        else:
            if not target.is_file():
                return f"Error: file not found: {rel}"
            try:
                text = _read_text(target)
            except PathError:
                return f"Error: {rel} is a binary file"
            old_lines = text.split("\n")
            if old_lines and old_lines[-1] == "":
                old_lines.pop()
        try:
            new_lines = _apply_hunks(old_lines, entry["hunks"])
        except ValueError as e:
            return f"Error: {rel}: {e}"
        if deleting:
            if new_lines:
                return f"Error: {rel}: deletion patch leaves content behind"
            plan.append((target, None, rel))
        else:
            new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
            plan.append((target, new_text, rel))
    done = []
    for target, new_text, rel in plan:
        try:
            if new_text is None:
                target.unlink()
                done.append(f"{rel} (deleted)")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_text, encoding="utf-8")
                done.append(rel)
        except OSError as e:
            return f"Error: cannot write {rel}: {e.strerror or e}"
    return "Applied patch to " + ", ".join(done)


# ---------------------------------------------------------------------- grep

def _walk_files(root: Path, workspace: Path):
    """Yield files under root (or root itself), skipping SKIP_DIRS and symlinks
    that leave the workspace, in sorted order."""
    if root.is_file():
        yield root
        return
    ws = workspace.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink():
                try:
                    r = p.resolve()
                except OSError:
                    continue
                if r != ws and ws not in r.parents:
                    continue
            if p.is_file():
                yield p


def grep(workspace: Path, args: dict, limits=None) -> str:
    try:
        rx = re.compile(args["pattern"])
    except re.error as e:
        return f"Error: invalid regular expression: {e}"
    try:
        root = resolve_path(workspace, args.get("path") or ".")
    except PathError as e:
        return f"Error: {e}"
    if not root.exists():
        return f"Error: path not found: {args.get('path')}"
    name_glob = args.get("glob")
    out: list[str] = []
    truncated = False
    for p in _walk_files(root, workspace):
        if name_glob and not fnmatch.fnmatch(p.name, name_glob):
            continue
        try:
            text = _read_text(p)
        except (PathError, OSError):
            continue
        rel = _rel(workspace, p)
        for no, line in enumerate(text.split("\n"), start=1):
            if rx.search(line):
                if len(out) >= GREP_MAX_MATCHES:
                    truncated = True
                    break
                out.append(f"{rel}:{no}:{line[:READ_MAX_LINE_CHARS]}")
        if truncated:
            break
    if not out:
        return "No matches."
    if truncated:
        out.append(f"[truncated at {GREP_MAX_MATCHES} matches; narrow the pattern or path]")
    return "\n".join(out)


# ---------------------------------------------------------------------- glob

def glob(workspace: Path, args: dict, limits=None) -> str:
    try:
        root = resolve_path(workspace, args.get("path") or ".")
    except PathError as e:
        return f"Error: {e}"
    if not root.is_dir():
        return f"Error: directory not found: {args.get('path')}"
    pattern = args["pattern"]
    if Path(pattern).is_absolute():
        return "Error: pattern must be relative"
    if ".." in pattern.replace("\\", "/").split("/"):
        return "Error: pattern must not contain .."
    ws = workspace.resolve()
    try:
        found = []
        for p in root.glob(pattern):
            r = p.resolve()
            if r != ws and ws not in r.parents:
                continue
            rel_parts = r.relative_to(ws).parts
            if rel_parts and rel_parts[0] in SKIP_DIRS:
                continue
            found.append("/".join(rel_parts) + ("/" if p.is_dir() else ""))
    except (ValueError, OSError) as e:
        return f"Error: bad glob pattern {pattern!r}: {e}"
    found = sorted(set(found))
    if not found:
        return "No files match."
    out = found[:LIST_MAX_ENTRIES]
    if len(found) > LIST_MAX_ENTRIES:
        out.append(f"[truncated: {len(found) - LIST_MAX_ENTRIES} more]")
    return "\n".join(out)


# ------------------------------------------------------------------------ ls

def ls(workspace: Path, args: dict, limits=None) -> str:
    rel = args.get("path") or "."
    try:
        root = resolve_path(workspace, rel)
    except PathError as e:
        return f"Error: {e}"
    if not root.exists():
        return f"Error: path not found: {rel}"
    if root.is_file():
        return f"Error: {rel} is a file; use read"
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError as e:
        return f"Error: cannot list {rel}: {e.strerror or e}"
    names = []
    for p in entries:
        if root.resolve() == workspace.resolve() and p.name in SKIP_DIRS:
            continue
        names.append(p.name + ("/" if p.is_dir() else ""))
    if not names:
        return "(empty directory)"
    out = names[:LIST_MAX_ENTRIES]
    if len(names) > LIST_MAX_ENTRIES:
        out.append(f"[truncated: {len(names) - LIST_MAX_ENTRIES} more]")
    return "\n".join(out)


# ---------------------------------------------------------------------- todo

def todo(workspace: Path, args: dict, limits=None) -> str:
    items = args["items"]
    clean = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "text" not in item or item.get("state") not in TODO_STATES:
            return f"Error: items[{i}] must be {{text, state}} with state in {list(TODO_STATES)}"
        clean.append({"text": str(item["text"]), "state": item["state"]})
    path = workspace / HARNESS_DIR / "todo.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return f"Error: cannot save todo list: {e.strerror or e}"
    if not clean:
        return "Todo list cleared."
    return "\n".join(f"{TODO_STATES[it['state']]} {it['text']}" for it in clean)


# --------------------------------------------------------------------- skill

def skill(workspace: Path, args: dict, limits=None) -> str:
    name = args["name"]
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\0" in name:
        return f"Error: invalid skill name {name!r}"
    try:
        path = resolve_path(workspace, f".agent/skills/{name}/SKILL.md")
    except PathError as e:
        return f"Error: skill {name!r}: {e}"
    if not path.is_file():
        return (f"Error: skill {name!r} not found "
                f"(expected .agent/skills/{name}/SKILL.md)")
    try:
        return _read_text(path)
    except PathError:
        return f"Error: skill {name!r} is not a text file"
    except OSError as e:
        return f"Error: cannot read skill {name!r}: {e.strerror or e}"


# ---------------------------------------------------------------------- bash

def bash_command(args: dict, limits=None) -> tuple[list[str], float]:
    """argv and timeout for a bash tool call. The timeout is args.timeout_s
    when given, else limits.op_timeout_s["guest"], else 120 s."""
    timeout = args.get("timeout_s")
    if not timeout:
        timeout = None
        if limits is not None:
            op = getattr(limits, "op_timeout_s", None)
            if isinstance(op, dict):
                timeout = op.get("guest")
        if not timeout:
            timeout = 120
    shell = shutil.which("bash") or "/bin/sh"
    return [shell, "-c", args["command"]], float(timeout)


IMPLS = {
    "read": read,
    "write": write,
    "edit": edit,
    "apply_patch": apply_patch,
    "grep": grep,
    "glob": glob,
    "ls": ls,
    "todo": todo,
    "skill": skill,
}
