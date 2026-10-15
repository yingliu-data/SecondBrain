"""guest.hooks — the before_tool / after_tool chains.

    before_tool(state, call) -> ("allow" | "deny" | "ask", reason)
    after_tool(state, call, content) -> str | None        a rewrite, or None
    Hooks                                                 ordered chains (nothing registers
                                                          a hook yet: the host sanitizes
                                                          its results in app/host/sides.py)
    Hooks.check_purity(state, call, content) -> None      runs both chains with the
                                                          clock, environment, randomness,
                                                          files and sockets patched to
                                                          raise; ImpureHook names the
                                                          hook that touched one
    purity_guard()                                        the context manager behind it
    ImpureHook(RuntimeError)

`call` is the tool_call body ({turn_id, call_id, name, side, args, ...}).

The defaults pass everything through: the before chain is empty and answers
("allow", "placeholder_pass"); the after chain is empty and rewrites nothing.
The coordinator still records a guard_decision for every call, so the
log shows the slot even while the policy is a placeholder.

Purity rule: a hook is a pure function of (state, call, content). It never
reads the clock, the environment, randomness, a file or the network, and it
never calls the LLM, a tool or the gateway. Replay re-runs nothing, so a hook
that depended on anything outside its arguments would make the log lie.
A hook reading time() fails the purity test.

How the rule is enforced (tests/test_guest_hooks.py): `check_purity` runs the
registered chains on a sample call inside `purity_guard()`, which patches the
plausible entry points a hook could reach for (FORBIDDEN: `time.time`/
`monotonic`/`perf_counter`, `os.environ`/`getenv`/`urandom`, `random.random`/
`randint`/`choice`/`shuffle` and `random.Random.random`, `socket.socket`/
`create_connection`, `open`) so that any use raises ImpureHook naming the hook
and the entry point. It is a test-time check of
the whole chain, not a sandbox: it catches the hook reading those things on
the sample call, and it patches process-wide, so it runs in tests only.
"""
from __future__ import annotations

import builtins
import os
import random
import socket
import time
from contextlib import contextmanager

PASS_REASON = "placeholder_pass"
DECISIONS = ("allow", "deny", "ask")

SAMPLE_CALL = {"turn_id": "turn_sample", "call_id": "call_sample", "name": "bash",
               "side": "guest", "args": {"command": "true"}}
SAMPLE_CONTENT = "sample tool output"


class ImpureHook(RuntimeError):
    """A hook reached for the clock, the environment, randomness, a file or
    the network. str() names the hook and the entry point."""

    def __init__(self, entry_point: str, hook: str = "?"):
        super().__init__(f"hook {hook} is impure: it used {entry_point}")
        self.entry_point = entry_point
        self.hook = hook


class _Forbidden:
    """A stand-in for os.environ: every read raises ImpureHook."""

    def __init__(self, what: str):
        self._what = what

    def __getattr__(self, name: str):
        if name.startswith("__"):           # protocol probes (copy, pickle, repr helpers)
            raise AttributeError(name)
        raise ImpureHook(self._what)

    def _raise(self, *a, **k):
        raise ImpureHook(self._what)

    # dunder lookups bypass __getattr__
    __getitem__ = __contains__ = __iter__ = __len__ = _raise


def _raiser(what: str):
    def fn(*a, **k):
        raise ImpureHook(what)
    return fn


# (owner, attribute): the entry points a hook could plausibly reach for. The
# module-level random functions are bound to an import-time instance, so
# `random.Random.random` is listed too for a hook that builds its own generator.
FORBIDDEN = (
    (time, "time"), (time, "monotonic"), (time, "perf_counter"),
    (os, "environ"), (os, "getenv"), (os, "urandom"),
    (random, "random"), (random, "randint"), (random, "choice"), (random, "shuffle"),
    (random.Random, "random"),
    (socket, "socket"), (socket, "create_connection"),
    (builtins, "open"),
)


def _label(owner, name: str) -> str:
    if owner is builtins:
        return name
    if isinstance(owner, type):
        return f"{owner.__module__}.{owner.__qualname__}.{name}"
    return f"{owner.__name__}.{name}"


@contextmanager
def purity_guard():
    """Inside the block, the FORBIDDEN entry points raise
    ImpureHook(entry_point). Patches process-wide: tests only.
    unittest.mock is imported here, not at module level: it costs ~40 ms
    and only this test-time check needs it (the guest imports hooks via the
    coordinator on every session)."""
    from unittest import mock

    patches = []
    for owner, name in FORBIDDEN:
        label = _label(owner, name)
        stand_in = _Forbidden(label) if (owner, name) == (os, "environ") else _raiser(label)
        patches.append(mock.patch.object(owner, name, stand_in))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


def _hook_name(fn) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


class Hooks:
    """Ordered chains. `before` hooks run in registration order and the first
    non-allow answer wins (a deny or an ask stops the chain); `after` hooks
    run in order, each seeing the previous rewrite, and the final text is the
    rewrite (None when no hook changed anything)."""

    def __init__(self):
        self._before: list = []
        self._after: list = []

    # registration
    def register_before(self, fn) -> None:
        self._before.append(fn)

    def register_after(self, fn) -> None:
        self._after.append(fn)

    @property
    def before_chain(self) -> list:
        return list(self._before)

    @property
    def after_chain(self) -> list:
        return list(self._after)

    # the two entry points
    def before_tool(self, state, call: dict) -> tuple[str, str]:
        """("allow", "placeholder_pass") from an empty chain; otherwise the
        first deny/ask, or the last hook's allow reason."""
        reason_out = PASS_REASON
        for fn in self._before:
            decision, reason = fn(state, call)
            if decision not in DECISIONS:
                raise ValueError(f"before_tool hook returned {decision!r}, expected one of {DECISIONS}")
            if decision != "allow":
                return decision, str(reason)
            reason_out = str(reason)
        return "allow", reason_out

    def after_tool(self, state, call: dict, content: str):
        text = content
        changed = False
        for fn in self._after:
            out = fn(state, call, text)
            if out is not None and out != text:
                text = out
                changed = True
        return text if changed else None

    # the purity check
    def check_purity(self, state=None, call: dict | None = None, content: str = SAMPLE_CONTENT) -> None:
        """Run every registered hook, one at a time, on (state, call, content)
        under purity_guard(). Raises ImpureHook naming the first hook that
        used a forbidden entry point; returns None when the chains are pure
        (the empty default chains are)."""
        call = dict(SAMPLE_CALL) if call is None else call
        runs = [(fn, (state, call)) for fn in self._before] + \
               [(fn, (state, call, content)) for fn in self._after]
        with purity_guard():
            for fn, args in runs:
                try:
                    fn(*args)
                except ImpureHook as exc:
                    raise ImpureHook(exc.entry_point, _hook_name(fn)) from None


DEFAULT = Hooks()


def before_tool(state, call: dict) -> tuple[str, str]:
    """Module-level pass-through: the default chain."""
    return DEFAULT.before_tool(state, call)


def after_tool(state, call: dict, content: str):
    return DEFAULT.after_tool(state, call, content)
