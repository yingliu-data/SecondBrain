"""guest.restore — resume from the returned log, a mixin of
guest.coordinator.Coordinator (docs/GUEST.md "Park, resume and restore").

restore(events) before run(): the log the host returned on :5001 after a
guest death (hello{resume: true}) goes through the same fold and the same
TaskView reducer as the live loop's emit(), so the loop's bookkeeping is what
the live loop would have left. Only the preamble registry is rebuilt on the
side (each regular turn's preamble text, from the state before its
turn_start). run() then finishes the open work (_resume_restored):
clock{host_hello}, the wall clock re-armed with what it has left, op_state
{lost} + an error tool_result for every open call, and the wake. A log that
ends with `parked` loses nothing: side calls and questions wait again,
clock{resumed} comes first, and in-flight mcp calls (never re-attached by a
fresh process) are lost.
"""
from __future__ import annotations

from protocol.canonical import hash_value, is_int
from protocol.fold import apply
from guest.context import preamble_text
from guest.taskview import RESUMED

LOST_CONTENT = "error: operation lost (VM restarted)"


class RestoreMixin:
    _restored: dict | None

    def restore(self, events: list) -> None:
        """Fold the log the host returned (hello{resume: true}, bulk get) into
        the state and the TaskView. Call before run(); run() then settles the
        open work (_resume_restored). Raises FoldError on a log that does not
        fold and ValueError on one that belongs elsewhere."""
        if self.state.last_seq:
            raise ValueError("restore() needs a fresh State")
        if not events:
            return
        if events[0].get("type") != "session_start" or events[0].get("session_id") != self.cfg.session_id:
            raise ValueError("the returned log does not start with this session's session_start")
        for ev in events:
            if ev["type"] == "turn_start" and ev["body"]["type"] == "regular":
                self._register_preamble(ev["body"])
            apply(self.state, ev)
            self._track(ev)
        view = self.view
        self.current_turn = view.open_turn
        self._uploaded = dict(view.settled_files)
        self._restore_mcp()
        max_tools = self.limits.max_tools
        self._final_turn = bool(view.task_input_id is not None and max_tools
                                and self._ops_this_task() >= max_tools)
        self._restored = {"after_park": view.parked,   # still in its stretch
                          "wall_used_ms": view.wall_used_ms, "live_since": view.live_since,
                          "park_question": view.park_question, "question_left_ms": view.question_left_ms}

    def _register_preamble(self, body: dict) -> None:
        """The preamble text a regular turn used, rebuilt from the state just
        before its turn_start (the turn_clock, the first staged input's
        attachments, the [new memory] slugs) and kept when its hash matches;
        the committed user message it marked renders with it."""
        first = next((it["message"].get("input_id") for it in self.state.staged_suffix
                      if it["message"].get("role") == "user"), None)
        text = preamble_text(self.state.clock_at or 0, self.view.attachments.get(first), self._new_memory())
        if hash_value(text) == body["preamble_hash"]:
            self._preambles[body["preamble_hash"]] = text
        elif first is not None:
            self.log("preamble of a restored turn could not be rebuilt", level="warning",
                     turn_id=body["turn_id"])

    def _resume_restored(self, restored: dict) -> None:
        """After restore(): record the host's time, settle what the dead guest
        left open and wake the task."""
        if restored["after_park"]:            # a wake from a park: its stretch ends here
            self.emit("clock", {"kind": RESUMED, "at": self.clock.now_ms()})
            self._restore_clocks(restored)
            self._resume_after_park(restored)
            return
        self._restore_clocks(restored)
        view = self.view
        lost_turn = self.current_turn
        if lost_turn is not None:
            self.current_turn = None
            self.emit("error", {"class": "transient", "message": "turn lost (VM restarted)",
                                "retry": True, "ref": lost_turn})
        open_calls = [cid for cid, call in sorted(self.state.open_calls.items(), key=lambda kv: kv[1]["seq"])
                      if "result" not in call]
        self._mcp_calls.clear()
        self._suppress_wake = True
        try:
            for call_id in open_calls:          # their questions and side waits close with them
                self._finish_call(call_id, "lost", LOST_CONTENT)
        finally:
            self._suppress_wake = False
        if view.task_input_id is None:
            return
        last = self.state.last_turn()
        if self._async:
            # every open call is lost now: unseen results and staged inputs wake
            # through _async_tick; otherwise the lost turn (or the task's first)
            # runs again, or the settle a dead guest owed is made
            if lost_turn is not None or not view.turns:
                self._start_turn()
            elif view.unseen or view.pending_inputs:
                pass
            elif last is not None and last["type"] == "regular" and last["response_seq"] is not None:
                self._finish_response(self._restored_message(last["response_seq"]),
                                      last.get("finish_reason"))
            else:
                self._start_turn()
            return
        if view.last_response_had_calls and not view.turn_pending:
            self._after_round()
            return
        if lost_turn is None and last is not None and last["type"] == "regular" \
                and last["response_seq"] is not None and not view.last_response_had_calls:
            self._finish_response(self._restored_message(last["response_seq"]),
                                  last.get("finish_reason"))
            return
        self._start_turn()

    def _restore_clocks(self, restored: dict) -> None:
        """The host's time, then the task's wall clock with what it has left."""
        if is_int(self.cfg.at, 1):
            self.emit("clock", {"kind": "host_hello", "at": self.cfg.at})
        if self.view.task_input_id is not None:
            self._cancel("idle")
            wall = self._task_wall_s()
            if wall:
                used_ms = restored["wall_used_ms"]
                if restored["live_since"] is not None:
                    used_ms += self._since(restored["live_since"])
                self._arm("wall_clock", "wall", max(1.0, float(wall) - used_ms / 1000.0))

    def _resume_after_park(self, restored: dict) -> None:
        """A fresh process after a park: nothing was lost; wait for the host again."""
        view = self.view
        for call_id in view.side_calls:
            self._arm_op_timeout(call_id)
        due = self._restored_question_deadlines(restored["question_left_ms"])
        # a fresh process never re-attaches: in-flight mcp calls are lost
        lost = [cid for cid, m in self._mcp_calls.items() if m["state"] == "inflight"]
        self._suppress_wake = True
        try:
            for cid in lost:
                self._finish_call(cid, "lost", LOST_CONTENT)
            for qid in due:             # past their due time: the normal expiry path
                if self.settled is not None:
                    break
                if view.questions[qid]["expired"]:
                    self._question_expired(qid)
                else:
                    self._expire_question(qid)
        finally:
            self._suppress_wake = False
        if restored["park_question"] in self._waiting:
            self._question_park_pending = False   # the log's own park is that park
        if self._async:
            # unseen results and inputs wake through _async_tick; only a first
            # turn the park deferred is started here
            if view.task_input_id is not None and self.settled is None \
                    and self.current_turn is None and not view.turns:
                self._start_turn()
            return
        if view.task_input_id is not None and self.settled is None \
                and self.current_turn is None and not view.turn_pending:
            if view.last_response_had_calls:
                self._after_round()
            else:
                self._start_turn()

    def _restored_question_deadlines(self, left_ms: dict) -> list:
        """After a restore of a parked log: re-arm each open question with the
        time it had left at the park (parked.question_left_ms; a parked stretch
        never counts, as live), and return the due ones, oldest first: its
        expiry already logged (`expired`) or nothing left. A question the park
        did not list had no deadline."""
        due = []
        for qid in sorted(self.view.questions, key=self._question_seq):
            left = left_ms.get(qid)
            if self.view.questions[qid]["expired"] or left == 0:
                due.append(qid)
            elif left is not None:
                self._arm_question(qid, left / 1000.0)
        return due

    def _restored_message(self, response_seq: int) -> dict:
        for it in [*self.state.staged_suffix, *self.state.committed]:
            if it["seq"] == response_seq:
                return it["message"]
        return {}
