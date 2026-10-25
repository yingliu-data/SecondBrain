"""The ast-everything tools for any agent: their definitions, in the OpenAI function format,
and `AstTools.call`, which runs one and answers with JSON text or `Error: ...`.

    from ast_everything.tools import AstTools
    tools = AstTools()                       # MONGODB_* from the environment or .env
    tools.definitions                        # give these to the model
    tools.call("ast_search", {"query": "setup"})

`ast_index` reads a folder or file on this machine (`path`), in the agent's workspace
(`workspace_path`, when the agent gives `workspace`) or a git repo it clones under
`repos_dir` (`git_url`), and indexes it as one source. An index can outlast a tool
timeout, so it runs as a background job in this process and `ast_index_status` reports
it; one job per source runs at a time, and the jobs are forgotten on restart (indexing
again is cheap: unchanged files are skipped). The read tools answer from the store
through `ast_everything.search`. A source the agent's `hidden` names is neither listed,
searched nor read, and the `memory-` prefix is kept for `ast_everything.memory`. A `path`
in or above one of the agent's `private_roots` (such as other users' data) is refused, and
a git URL's credentials never reach a job's `target` or `error`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pymongo.errors import OperationFailure, PyMongoError

from ast_everything.indexer import index_files, source_name, sync, valid_source
from ast_everything.memory import MEMORY_PREFIX
from ast_everything.parsers import parser_for
from ast_everything.search import by_time, retrieve, search, sources, traverse
from ast_everything.store import Store

logger = logging.getLogger(__name__)

AUTHENTICATION_FAILED = 18     # MongoDB's error code
GIT_TIMEOUT_S = 600
GIT_SCHEMES = ("https", "ssh")
DETAILS = ("fold", "preview", "full")
DIRECTIONS = ("out", "in", "both")


class ToolError(Exception):
    """An error the model sees as the tool's answer."""


@dataclass
class Job:
    id: str
    source: str
    target: str
    state: str = "running"          # running, done, failed
    counts: dict = field(default_factory=dict)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def view(self) -> dict:
        out = {"job_id": self.id, "source": self.source, "target": self.target, "state": self.state}
        if self.counts:
            out["counts"] = self.counts
        if self.error:
            out["error"] = self.error
        end = self.finished_at or time.time()
        out["seconds"] = round(end - self.started_at, 1)
        return out


def _function(name: str, description: str, properties: dict, required: list[str] = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


STR_LIST = {"type": "array", "items": {"type": "string"}}
TIME_RANGE = {"type": "array", "items": {"type": ["string", "null"]}, "minItems": 2, "maxItems": 2,
              "description": "[start, end] in ISO 8601; either one may be null"}
SCOPE = {"type": "string", "description": "the source (one indexed project), as ast_sources lists them"}
DETAIL = {"type": "string", "enum": list(DETAILS)}

TOOLS = [
    _function(
        "ast_index",
        "Parse a project or documents (Python and Markdown files) into AST-style nodes and edges and "
        "save them to MongoDB, as one source. Give exactly one of `path` (a folder or file on the server), "
        "`workspace_path` (a folder or file in this session's workspace, as of the end of the last task) "
        "or `git_url` (an https or ssh repo, cloned on the server). Runs in the background: returns a "
        "job_id for ast_index_status. Indexing again only re-parses changed files; a folder sync also "
        "marks files that are gone as removed. Atlas embeds each new class, function, document and "
        "section version, which is billed per token.",
        {"path": {"type": "string", "description": "an absolute path on the server"},
         "workspace_path": {"type": "string", "description": "a path relative to the session workspace"},
         "git_url": {"type": "string"},
         "name": {"type": "string", "description": "the source name, not starting with memory- (default: made from the folder or repo name)"}}),
    _function(
        "ast_index_status",
        "The state of an ast_index job (running, done or failed), its counts, or its error. "
        "Without job_id: every job of this server since it started.",
        {"job_id": {"type": "string"}}),
    _function(
        "ast_sources",
        "The indexed sources (projects and document sets in MongoDB), with their file counts.",
        {}),
    _function(
        "ast_search",
        "Find nodes in the index: by exact ID (`proj/pkg/utils.py:MathUtils.double`, `proj/docs/a.md#setup`), "
        "else by name (a file, class, function or heading), else by keyword over names, else by content "
        "(keyword and meaning). The first layer with hits answers. `node_types`: directory, file, class, "
        "function, document, section, paragraph, list, list_item, table, code, quote, html, session, turn. "
        "Past chat sessions (session and turn nodes) and the user's memory files are the source "
        "`memory-<user>`; every source but other users' memory is searched unless `scope` names one. "
        "`history` also shows superseded and removed versions. `detail`: fold (ID, type, location, "
        "status), preview "
        "(adds the first lines), full (adds the text).",
        {"query": {"type": "string"}, "scope": SCOPE, "node_types": STR_LIST,
         "path_prefix": {"type": "string"}, "time_range": TIME_RANGE, "history": {"type": "boolean"},
         "detail": DETAIL, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["query"]),
    _function(
        "ast_traverse",
        "The nodes within `hops` edges of `start_ids` (IDs from ast_search), and those edges. Edge types: "
        "contains, imports, invokes, inherits, links_to, precedes (a turn to the next), supersedes. `out` "
        "follows edges from the start, `in` edges into it, `both` either way; at most `max_fanout` edges "
        "per node.",
        {"start_ids": STR_LIST, "direction": {"type": "string", "enum": list(DIRECTIONS)},
         "hops": {"type": "integer", "minimum": 1, "maximum": 5}, "node_types": STR_LIST,
         "edge_types": STR_LIST, "max_fanout": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["start_ids"]),
    _function(
        "ast_retrieve",
        "The text of each ID with its line range: a version ID as it was, a logical ID as it is now. "
        "A long text comes in pages of 200 lines (`page`); a superseded version names the newest one; "
        "`context_lines` adds lines around a method from its class.",
        {"ids": STR_LIST, "detail": DETAIL, "context_lines": {"type": "integer", "minimum": 0, "maximum": 100},
         "page": {"type": "integer", "minimum": 1}},
        ["ids"]),
    _function(
        "ast_search_by_time",
        "What was added, changed or removed in `time_range`, newest change first, with when it changed.",
        {"time_range": TIME_RANGE, "scope": SCOPE, "node_types": STR_LIST,
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["time_range"]),
]


class AstTools:
    """The tools over one store. `connect` makes the store (default: `Store.connect`, the
    MONGODB_* variables); `repos_dir` holds the git clones (default: `AST_REPOS_DIR`, else
    `data/ast-everything/repos`); `workspace()` is the folder `workspace_path` resolves in,
    or None; `hidden(store)` names the sources the caller may not read; `private_roots` are
    folders a `path` may neither be in nor hold."""

    definitions = TOOLS

    def __init__(self, connect: Callable[[], Store] = Store.connect, repos_dir: str | Path | None = None,
                 workspace: Callable[[], str | Path | None] = lambda: None,
                 hidden: Callable[[Store], Iterable[str]] = lambda store: (),
                 private_roots: Iterable[str | Path] = ()):
        self._connect = connect
        self.repos_dir = Path(repos_dir or os.environ.get("AST_REPOS_DIR") or "data/ast-everything/repos")
        self._workspace = workspace
        self._hidden_sources = hidden
        self._private_roots = [Path(root).resolve() for root in private_roots]
        self._store: Store | None = None
        self._indexes_ready = False
        self._lock = threading.Lock()
        self.jobs: dict[str, Job] = {}

    def call(self, tool_name: str, arguments: dict | None) -> str:
        """Run one tool: its result as JSON text, or `Error: ...`."""
        handler = getattr(self, f"_{tool_name}", None)
        if handler is None or not tool_name.startswith("ast_"):
            return f"Error: no tool '{tool_name}'."
        try:
            result = handler(dict(arguments or {}))
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:       # noqa: BLE001 - a store failure is the tool's answer
            return f"Error: {_store_error(exc)}"
        return json.dumps(result, ensure_ascii=False, default=str)

    # ------------------------------------------------------------ the store
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = self._connect()
            return self._store

    def _writable_store(self) -> Store:
        store = self.store()
        with self._lock:
            if not self._indexes_ready:
                store.prepare()
                self._indexes_ready = True
        return store

    # ------------------------------------------------------------ indexing
    def _ast_index(self, args: dict) -> dict:
        given = [key for key in ("path", "workspace_path", "git_url") if args.get(key)]
        if len(given) != 1:
            raise ToolError("give exactly one of path, workspace_path or git_url")
        key = given[0]
        value = str(args[key]).strip()
        if key == "git_url":
            dest, default_name = self._clone_target(value)
            target, prepare = dest, (lambda: self._clone(value, dest))
        else:
            target = self._local_path(key, value)
            default_name, prepare = target.stem if target.is_file() else target.name, None
        source = _source(args.get("name"), default_name)
        with self._lock:
            for job in self.jobs.values():
                if job.source == source and job.state == "running":
                    return {**job.view(), "note": "an index of this source is already running"}
            job = Job(id=uuid.uuid4().hex[:12], source=source,
                      target=_redacted(value) if key == "git_url" else value)
            self.jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job, target, prepare),
                         name=f"ast-index-{job.id}", daemon=True).start()
        return job.view()

    def _run_job(self, job: Job, target: Path, prepare) -> None:
        try:
            if prepare is not None:
                prepare()
            store = self._writable_store()
            if target.is_file():
                counts = index_files(store, job.source, target.parent, [target])
            else:
                counts = sync(store, job.source, target)
            job.counts, job.state = dict(counts), "done"
        except Exception as exc:       # noqa: BLE001 - the job reports what went wrong
            logger.warning("ast_index job %s (%s) failed: %s", job.id, job.source, exc)
            job.error, job.state = _store_error(exc), "failed"
        finally:
            job.finished_at = time.time()

    def _local_path(self, key: str, value: str) -> Path:
        if key == "path":
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ToolError(f"path must be absolute: {value}")
            path = path.resolve()
            if any(path.is_relative_to(root) or root.is_relative_to(path) for root in self._private_roots):
                raise ToolError(f"path is in or holds a private folder: {value}")
        else:
            workspace = self._workspace()
            if workspace is None:
                raise ToolError("workspace_path needs a session")
            workspace = Path(workspace).resolve()
            relative = value.removeprefix("/workspace/").lstrip("/") or "."
            path = (workspace / relative).resolve()
            if not path.is_relative_to(workspace):
                raise ToolError(f"workspace_path is outside the workspace: {value}")
        if not path.exists():
            raise ToolError(f"no such file or folder: {value}")
        if path.is_file() and parser_for(path.name) is None:
            raise ToolError(f"no parser for {path.name}: Python (.py) and Markdown (.md, .markdown) are read")
        return path

    def _clone_target(self, url: str) -> tuple[Path, str]:
        parts = urlsplit(url)
        scp_like = not parts.scheme and "@" in url and ":" in url     # git@host:owner/repo.git
        if parts.scheme not in GIT_SCHEMES and not scp_like:
            raise ToolError(f"git_url must be an https or ssh repo URL: {url}")
        repo = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git") or "repo"
        name = source_name(repo)
        digest = hashlib.sha256(url.encode()).hexdigest()[:8]
        return (self.repos_dir / f"{name}-{digest}").resolve(), name

    def _clone(self, url: str, dest: Path) -> None:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if (dest / ".git").is_dir():
            commands = [["git", "-C", str(dest), "fetch", "--depth", "1", "origin"],
                        ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"]]
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            commands = [["git", "clone", "--depth", "1", "--", url, str(dest)]]
        for command in commands:
            done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
            if done.returncode != 0:
                verb = "clone" if "clone" in command else command[3]
                output = (done.stderr or done.stdout).replace(url, _redacted(url)).strip()[-500:]
                raise RuntimeError(f"git {verb} failed: {output}")

    def _ast_index_status(self, args: dict) -> dict:
        job_id = args.get("job_id")
        if not job_id:
            return {"jobs": [job.view() for job in self.jobs.values()]}
        job = self.jobs.get(job_id)
        if job is None:
            raise ToolError(f"unknown job_id {job_id} (jobs are forgotten when the server restarts)")
        return job.view()

    # ------------------------------------------------------------ reading
    def _hidden(self, *names: str | None) -> list[str]:
        """The sources `hidden` names; a scope or an ID in one of them is refused."""
        hidden = list(self._hidden_sources(self.store()))
        if any(name.split("/")[0] in hidden for name in names if name):
            raise ToolError("this source cannot be read here")
        return hidden

    def _ast_sources(self, args: dict) -> dict:
        hidden = self._hidden()
        return {"sources": [row for row in sources(self.store()) if row["source"] not in hidden]}

    def _ast_search(self, args: dict) -> dict:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query is required")
        result = search(self.store(), query, scope=args.get("scope"), node_types=_strings(args, "node_types"),
                        path_prefix=args.get("path_prefix"), time_range=_time_range(args, required=False),
                        history=bool(args.get("history")), detail=_choice(args, "detail", DETAILS, "preview"),
                        limit=_int(args, "limit", 10, 1, 100), hide=self._hidden(args.get("scope")))
        if result["layer"] is None:
            result["message"] = "no layer matched"
        return result

    def _ast_traverse(self, args: dict) -> dict:
        start_ids = _strings(args, "start_ids")
        if not start_ids:
            raise ToolError("start_ids is required")
        self._hidden(*start_ids)
        result = traverse(self.store(), start_ids, direction=_choice(args, "direction", DIRECTIONS, "out"),
                          hops=_int(args, "hops", 1, 1, 5), node_types=_strings(args, "node_types"),
                          edge_types=_strings(args, "edge_types"), max_fanout=_int(args, "max_fanout", 20, 1, 100))
        if not result["nodes"]:
            raise ToolError(f"unknown IDs: {', '.join(start_ids)}; find one with ast_search")
        return result

    def _ast_retrieve(self, args: dict) -> dict:
        ids = _strings(args, "ids")
        if not ids:
            raise ToolError("ids is required")
        self._hidden(*ids)
        return {"results": retrieve(self.store(), ids, detail=_choice(args, "detail", DETAILS, "full"),
                                    context_lines=_int(args, "context_lines", 0, 0, 100),
                                    page=_int(args, "page", 1, 1, 1_000_000))}

    def _ast_search_by_time(self, args: dict) -> dict:
        return {"results": by_time(self.store(), _time_range(args, required=True), scope=args.get("scope"),
                                   node_types=_strings(args, "node_types"), limit=_int(args, "limit", 50, 1, 100),
                                   hide=self._hidden(args.get("scope")))}


def _redacted(url: str) -> str:
    """`url` without the user info that may hold a password or token (`https://user:token@host`,
    `https://token@host`); an ssh user name alone (`ssh://git@host`) stays."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    user, _, host = parts.netloc.rpartition("@")
    if parts.scheme == "ssh" and ":" not in user:
        return url
    return parts._replace(netloc=f"***@{host}").geturl()


def _source(name, default: str) -> str:
    try:
        source = valid_source(name) if name else source_name(default)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if source.startswith(MEMORY_PREFIX):
        raise ToolError(f"source name {source!r}: the memory- prefix is kept for the memory index")
    return source


def _int(args: dict, name: str, default: int, low: int, high: int) -> int:
    value = args.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be an integer from {low} to {high}")
    if not low <= value <= high:
        raise ToolError(f"{name} must be from {low} to {high}, not {value}")
    return value


def _choice(args: dict, name: str, choices: tuple, default: str) -> str:
    value = args.get(name) or default
    if value not in choices:
        raise ToolError(f"{name} must be one of {', '.join(choices)}")
    return value


def _strings(args: dict, name: str) -> list[str] | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{name} must be a list of strings")
    return value


def _time_range(args: dict, *, required: bool) -> list | None:
    value = args.get("time_range")
    if value is None and not required:
        return None
    if not (isinstance(value, list) and len(value) == 2 and all(v is None or isinstance(v, str) for v in value)):
        raise ToolError("time_range must be [start, end] in ISO 8601, either one null")
    return value


def _store_error(exc: Exception) -> str:
    """What a store failure tells the model; a missing setting names the variable."""
    if isinstance(exc, OperationFailure) and exc.code == AUTHENTICATION_FAILED:
        return "MongoDB authentication failed: check MONGODB_USERNAME and MONGODB_PASSWORD"
    if isinstance(exc, OperationFailure):
        return f"MongoDB query failed: {exc}"
    if isinstance(exc, PyMongoError):
        return f"MongoDB is not reachable: {exc}"
    return str(exc) or type(exc).__name__
