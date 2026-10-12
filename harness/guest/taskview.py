"""guest.taskview — the coordinator's derived task bookkeeping, as one reducer.

    TaskView(cfg, now_ms)
        .apply(state, event)     after protocol.fold.apply(state, event)

The fold (protocol.fold.State) is what the log means; TaskView is what the
coordinator's loop needs on top of it to decide the next step: the open task
and its baselines, the round, the waits, the doom-loop counts. Every field is
a function of the events alone (plus the config and, for a side call's start,
the guest clock when the event is applied), so the live loop and restore()
share it: Coordinator.emit() applies each event it writes, restore() each
event of the returned log, and the two end in the same TaskView. Nothing here
is hashed: checkpoints cover the fold's State only.

What the loop keeps outside it is what the log does not say: timers, the mcp
op lifecycle (guest.mcp_ops), the provider call in flight, the questions past
their deadline it is parked on (a zero-timeout question expires without a
clock event), the park request, the usage correction.
"""
from __future__ import annotations

from protocol.canonical import canonical_json, is_int
from protocol.events import OP_INPUT_REQUIRED, UNANSWERED

DOOM_LOOP_REPEATS = 3
QUESTION_TOOL = "question"
INPUT_KINDS = ("task", "follow_up", "steer")
HOST_CLOCK_KINDS = ("host_hello", "host_heartbeat")   # clock events in host time, not the guest's
RESUMED = "resumed"               # clock kind written first on every wake from a park (ends its stretch)
SIDE_CALL_SIDES = ("host", "device")   # calls that wait for a side_result


def call_key(name: str, args: dict) -> str:
    """The doom-loop key of a call: its name and canonical arguments."""
    return name + ":" + canonical_json(args)


class TaskView:
    def __init__(self, cfg, now_ms):
        self.cfg = cfg
        self._now_ms = now_ms
        self._async = cfg.async_tools

        # the task (one at a time; None between tasks)
        self.task_input_id: str | None = None
        self.usage_base: dict = {}              # state.usage when it started
        self.ops_base = 0                       # state.limits_consumed["ops"] when it started
        self.turns = 0                          # regular turns started in it
        self.user_memory: list[str] = []        # user-scope slugs it wrote

        # session-wide registries (input_id -> ...)
        self.attachments: dict[str, list] = {}  # what a task message delivered
        self.task_walls: dict[str, int] = {}    # the wall clock it named (task.wall_clock_s)

        # the round
        self.pending_inputs: list[str] = []     # input_ids staged since the last regular turn_start
        self.unseen: list[str] = []             # call_ids with a result since then
        self.turn_calls: list[str] = []         # call_ids of the current round, in order
        self.turn_pending: set[str] = set()     # open calls (async: every open call of the task)
        self.last_response_had_calls = False
        self.open_turn: str | None = None       # a turn_start without its llm_response

        # waits
        self.calls: dict[str, dict] = {}        # open call_id -> {name, side, args, guard_ask, asked}
        self.side_calls: dict[str, dict] = {}   # call_id -> {side, timeout_s, started_ms}
        self.questions: dict[str, dict] = {}    # question_id -> {call_id, kind, name, args, side, expired}
        self.call_counts: dict[str, int] = {}   # call_key -> calls since the last doom-loop ask
        self._guard: tuple | None = None        # (policy_id, decision) of the event just before

        # park accounting: the task's wall clock used = wall_used_ms (the last
        # park's) + the guest-clock time since live_since (the clock{resumed}
        # that ended the stretch, or the task's first guest clock event)
        self.wall_used_ms = 0
        self.live_since: int | None = None
        self.in_stretch = False                 # between a parked and its clock{resumed}
        self.parked = False                     # the newest event is `parked`
        self.park_question: str | None = None   # the last park's question_id
        self.question_left_ms: dict = {}        # the last park's question_left_ms

        self.settled_files: dict[str, str] = {}  # path -> sha256 of the last settle's manifest

    # ------------------------------------------------------------ queries
    def guest_running(self, mcp_calls) -> set:
        """Open calls that run as guest operations: not side calls waiting for
        side_result, not asked, not the mcp ops in `mcp_calls`."""
        asked = {q["call_id"] for q in self.questions.values()}
        return {c for c in self.turn_pending
                if c not in self.side_calls and c not in asked and c not in mcp_calls}

    def doom_loop(self, name: str, args: dict) -> bool:
        """True when one more call with this name and arguments is a doom loop."""
        return self.call_counts.get(call_key(name, args), 0) + 1 >= DOOM_LOOP_REPEATS

    # ------------------------------------------------------------ the reducer
    def apply(self, state, event: dict) -> None:
        """Fold one event (already applied to `state`) into the view."""
        etype = event["type"]
        guard, self._guard = self._guard, None
        self.parked = etype == "parked"
        handler = _HANDLERS.get(etype)
        if handler is not None:
            handler(self, state, event["body"], guard)

    def _input(self, state, body, guard) -> None:
        if body["dedupe"] != "new":
            return
        if body["kind"] == "ui_response":
            self.questions.pop(body.get("question_id"), None)
            return
        if body["kind"] not in INPUT_KINDS:
            return
        input_id = body["input_id"]
        if isinstance(body.get("attachments"), list):
            self.attachments[input_id] = body["attachments"]
        if is_int(body.get("wall_clock_s"), 1):
            self.task_walls[input_id] = body["wall_clock_s"]
        self.pending_inputs.append(input_id)
        if self.task_input_id is None:
            self.task_input_id = input_id
            self.usage_base = dict(state.usage)
            self.ops_base = state.limits_consumed["ops"]
            self.turns = 0
            self.user_memory = []
            self.wall_used_ms, self.live_since = 0, None

    def _clock(self, state, body, guard) -> None:
        kind = body["kind"]
        if kind in HOST_CLOCK_KINDS:
            return
        self.in_stretch = self.in_stretch and kind != RESUMED
        if self.live_since is None and not self.in_stretch and self.task_input_id is not None:
            self.live_since = body["at"]
        if kind == "question_deadline" and body.get("ref") in self.questions:
            self.questions[body["ref"]]["expired"] = True

    def _parked(self, state, body, guard) -> None:
        self.wall_used_ms = body.get("wall_used_ms", self.wall_used_ms)
        self.live_since, self.in_stretch = None, True
        self.park_question = body.get("question_id")
        self.question_left_ms = body.get("question_left_ms", {})

    def _turn_start(self, state, body, guard) -> None:
        self.open_turn = body["turn_id"]
        if body["type"] == "regular":
            self.turns += 1
            self.turn_calls = []
            if not self._async:               # async: calls run on across turns
                self.turn_pending = set()
            self.pending_inputs, self.unseen = [], []

    def _llm_response(self, state, body, guard) -> None:
        self.open_turn = None
        self.last_response_had_calls = False

    def _guard_decision(self, state, body, guard) -> None:
        if body["decision"] == "deny" and body["reason"] == UNANSWERED:
            self.questions.pop(body["ref"], None)
        self._guard = (body["policy_id"], body["decision"])

    def _tool_call(self, state, body, guard) -> None:
        call_id = body["call_id"]
        self.calls[call_id] = {"name": body["name"], "side": body["side"], "args": body["args"],
                               "guard_ask": guard is not None and guard[1] == "ask", "asked": False}
        self.turn_calls.append(call_id)
        self.turn_pending.add(call_id)
        self.last_response_had_calls = True
        if guard is not None and guard[0] == "hooks":     # the calls the doom-loop check saw
            key = call_key(body["name"], body["args"])
            self.call_counts[key] = self.call_counts.get(key, 0) + 1
            if self.call_counts[key] >= DOOM_LOOP_REPEATS:
                self.call_counts[key] = 0

    def _op_state(self, state, body, guard) -> None:
        call = self.calls.get(body["call_id"])
        if body["state"] == "running" and body.get("side_ref") and call is not None \
                and call["side"] in SIDE_CALL_SIDES:
            self.side_calls[body["call_id"]] = {
                "side": call["side"], "timeout_s": self.cfg.op_timeout_for(call["name"], call["side"]),
                "started_ms": self._now_ms()}

    def _question(self, state, body, guard) -> None:
        call_id = body["call_id"]
        call = self.calls.get(call_id, {})
        if state.open_calls[call_id]["state"] == OP_INPUT_REQUIRED:
            kind = "mcp_input"
        elif call.get("guard_ask") and not call.get("asked"):
            kind = "ask"                      # the guard's question comes before the call runs
        else:
            kind = "question" if call.get("name") == QUESTION_TOOL else "ask"
        if call:
            call["asked"] = True
        self.questions[body["question_id"]] = {
            "call_id": call_id, "kind": kind, "name": call.get("name"),
            "args": call.get("args", {}), "side": call.get("side", "guest"), "expired": False}

    def _tool_result(self, state, body, guard) -> None:
        call_id = body["call_id"]
        self.turn_pending.discard(call_id)
        self.unseen.append(call_id)
        self.side_calls.pop(call_id, None)
        self.calls.pop(call_id, None)
        # a question whose call has its result is closed (answered, denied or abandoned)
        for qid in [q for q, v in self.questions.items() if v["call_id"] == call_id]:
            del self.questions[qid]

    def _wake(self, state, body, guard) -> None:
        if body["reason"] == "tool_result":
            self.last_response_had_calls = False

    def _memory(self, state, body, guard) -> None:
        if body["scope"] != "user":
            return
        slug = body["slug"]
        if body["op"] == "write" and slug not in self.user_memory:
            self.user_memory.append(slug)
        elif body["op"] == "remove" and slug in self.user_memory:
            self.user_memory.remove(slug)

    def _settle(self, state, body, guard) -> None:
        self.task_input_id = None
        self.pending_inputs, self.unseen = [], []
        self.turn_calls, self.turn_pending = [], set()
        self.last_response_had_calls = False
        if isinstance(body.get("manifest"), dict):
            self.settled_files = {f["path"]: f["sha256"] for f in body["manifest"].get("files", [])
                                  if isinstance(f, dict) and "path" in f and "sha256" in f}


_HANDLERS = {etype: getattr(TaskView, "_" + etype) for etype in (
    "input", "clock", "parked", "turn_start", "llm_response", "guard_decision", "tool_call",
    "op_state", "question", "tool_result", "wake", "memory", "settle")}
