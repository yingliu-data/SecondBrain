"""guest.hooks — the chains and the purity rule (a hook reading time() makes
the purity test fail)."""
from __future__ import annotations

import os
import random
import socket
import time

import pytest

from guest import hooks as hooks_mod
from guest.hooks import Hooks, ImpureHook, purity_guard


def _allow(state, call):
    return "allow", "ok"


# ---------------------------------------------------------------- chains
def test_default_chains_pass_through():
    h = Hooks()
    assert h.before_tool(None, {"name": "bash"}) == ("allow", hooks_mod.PASS_REASON)
    assert h.after_tool(None, {"name": "bash"}, "out") is None
    assert hooks_mod.before_tool(None, {}) == ("allow", hooks_mod.PASS_REASON)
    assert hooks_mod.after_tool(None, {}, "x") is None


def test_first_non_allow_wins_and_rewrites_chain():
    h = Hooks()
    h.register_before(_allow)
    h.register_before(lambda s, c: ("deny", "policy") if c["name"] == "bash" else ("allow", "fine"))
    h.register_before(lambda s, c: ("ask", "never reached"))
    assert h.before_tool(None, {"name": "bash"}) == ("deny", "policy")
    assert h.before_tool(None, {"name": "read"}) == ("ask", "never reached")
    h.register_after(lambda s, c, t: t.replace("secret", "***"))
    h.register_after(lambda s, c, t: t.upper())
    assert h.after_tool(None, {}, "a secret") == "A ***"
    assert h.after_tool(None, {}, "plain") == "PLAIN"
    h2 = Hooks()
    h2.register_after(lambda s, c, t: None)
    assert h2.after_tool(None, {}, "same") is None


def test_bad_decision_is_an_error():
    h = Hooks()
    h.register_before(lambda s, c: ("maybe", "?"))
    with pytest.raises(ValueError, match="maybe"):
        h.before_tool(None, {})


# ---------------------------------------------------------------- purity
def test_default_chains_are_pure():
    Hooks().check_purity()
    hooks_mod.DEFAULT.check_purity()


def test_pure_hooks_pass_the_check():
    h = Hooks()
    h.register_before(lambda s, c: ("deny", "no bash") if c["name"] == "bash" else ("allow", "ok"))
    h.register_after(lambda s, c, t: t.replace("\x00", ""))
    h.check_purity()
    h.check_purity(state=object(), call={"name": "read", "args": {}}, content="x")


@pytest.mark.parametrize("name, impure, entry", [
    ("time", lambda s, c: ("allow", str(time.time())), "time.time"),
    ("monotonic", lambda s, c: ("allow", str(time.monotonic())), "time.monotonic"),
    ("environ", lambda s, c: ("allow", os.environ.get("HOME", "")), "os.environ"),
    ("environ_item", lambda s, c: ("allow", os.environ["PATH"]), "os.environ"),
    ("getenv", lambda s, c: ("allow", os.getenv("HOME") or ""), "os.getenv"),
    ("random", lambda s, c: ("allow", str(random.random())), "random.random"),
    ("randint", lambda s, c: ("allow", str(random.randint(1, 9))), "random.randint"),
    ("random_instance", lambda s, c: ("allow", str(random.Random(1).random())), "random.Random.random"),
    ("urandom", lambda s, c: ("allow", os.urandom(2).hex()), "os.urandom"),
    ("socket", lambda s, c: ("allow", repr(socket.socket())), "socket.socket"),
    ("connect", lambda s, c: ("allow", repr(socket.create_connection(("127.0.0.1", 9)))), "socket.create_connection"),
    ("open", lambda s, c: ("allow", open("/etc/hostname").read()), "open"),
])
def test_impure_before_hook_fails_the_purity_check(name, impure, entry):
    h = Hooks()
    h.register_before(_allow)
    h.register_before(impure)
    with pytest.raises(ImpureHook) as info:
        h.check_purity()
    assert info.value.entry_point == entry
    assert "<lambda>" in str(info.value) and entry in str(info.value)


def test_impure_after_hook_fails_the_purity_check():
    def stamp(state, call, content):
        return f"{content} @ {time.time()}"
    h = Hooks()
    h.register_after(stamp)
    with pytest.raises(ImpureHook) as info:
        h.check_purity()
    assert info.value.hook == "test_impure_after_hook_fails_the_purity_check.<locals>.stamp"
    assert info.value.entry_point == "time.time"


def test_purity_guard_is_scoped():
    before = time.time()
    with purity_guard():
        with pytest.raises(ImpureHook):
            time.time()
        with pytest.raises(ImpureHook):
            os.environ.get("HOME")
    assert time.time() >= before                  # restored
    assert os.environ.get("PATH") is not None
    assert isinstance(random.random(), float)
