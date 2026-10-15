"""guest.questions — questions and the question park, a mixin of
guest.coordinator.Coordinator (docs/GUEST.md "Questions that park the task").

A question is the `question` built-in, a guard "ask" (doom loop or a hook) or
an mcp input request; TaskView.questions holds the open ones (kind, call).
Its deadline is a question_deadline timer. answer_wait_s == 0: a deadline is
guard_decision{deny, unanswered} (MAX_UNANSWERED of them escalate).
answer_wait_s > 0: the question stays open and the task parks on it
(`_waiting`, `_question_park_pending`; the park itself is guest.park's).
"""
from __future__ import annotations

from protocol.events import UNANSWERED

MAX_UNANSWERED = 3
CONTINUE_ANSWERS = ("continue", "yes", "y", "ok", "go", "proceed", "true")


class QuestionsMixin:
    _waiting: set
    _question_park_pending: bool

    def _question_timeout(self, expires=None) -> float:
        """A question's timeout: cfg.question_timeout_s, capped by a gateway
        input request's expires_in_s; 0 = no deadline."""
        timeout_s = self.cfg.question_timeout_s or 0
        if expires:
            timeout_s = min(timeout_s, expires) if timeout_s > 0 else expires
        return timeout_s

    def _arm_question(self, qid: str, delay_s: float) -> None:
        """Arm a question's deadline (none when `delay_s` is 0)."""
        if delay_s > 0:
            self._arm("question_deadline", f"question:{qid}", float(delay_s))

    def _ask_tool(self, call_id: str, args: dict) -> None:
        """The `question` built-in: its text and options from the arguments."""
        text = args.get("text") or args.get("question") or args.get("prompt") or ""
        options = args.get("options") or []
        options = [str(o) for o in options] if isinstance(options, list) else []
        self._ask(call_id, text=str(text), options=options)

    def _ask(self, call_id: str, *, text: str, options: list, timeout_s=None) -> None:
        """question{...} for a call; its kind (question, ask, mcp_input) is
        TaskView's, from the log."""
        if timeout_s is None:
            timeout_s = self._question_timeout()
        now = self.clock.now_ms()
        deadline = now + int(timeout_s * 1000)
        qid = self._id("question")
        self.emit("question", {"question_id": qid, "call_id": call_id, "text": text,
                               "options": options, "deadline": deadline})
        if timeout_s <= 0 and not self.cfg.ui_attached:
            self._question_expired(qid)
            return
        self._arm_question(qid, timeout_s)

    def _on_ui_response(self, body: dict) -> None:
        input_id = body.get("input_id") or f"ui:{body.get('question_id')}"
        qid = body.get("question_id")
        answer = body.get("answer", "")
        answer = "" if answer is None else str(answer)
        base = {"input_id": input_id, "kind": "ui_response", "text": answer,
                "question_id": str(qid), "answer": answer}
        if input_id in self.state.seen_input_ids:
            self.emit("input", {**base, "dedupe": "duplicate"})
            return
        q = self.state.open_questions.get(qid)
        if q is None or q["answered"]:
            self.emit("input", {**base, "dedupe": "late"})
            self.log(f"ui_response for unknown or answered question {qid!r} ignored", level="warning")
            return
        pending = self.view.questions.get(qid)
        self.emit("input", {**base, "dedupe": "new"})        # TaskView closes the question
        self._cancel(f"question:{qid}")
        self._waiting.discard(qid)
        if not self._waiting:
            self._question_park_pending = False   # answered before its park happened
        if pending is None:
            return
        call_id = pending["call_id"]
        if pending["kind"] == "mcp_input":
            self._mcp_answer(call_id, answer)
        elif pending["kind"] == "ask":
            # a guard "ask" (doom loop or a hook): continue runs the original call
            if answer.strip().lower() in CONTINUE_ANSWERS:
                self._run_call(call_id, pending["name"], pending["args"], pending["side"])
            else:
                self._finish_call(call_id, "failed", f"stopped by user: {answer}")
        else:
            self._finish_call(call_id, "done", answer)
        self._park_next_waiting()

    def _question_seq(self, qid: str) -> int:
        return self.state.open_questions[qid]["seq"]

    def _oldest_waiting(self) -> str:
        return min(self._waiting, key=self._question_seq)

    def _request_question_park(self, message: str) -> None:
        """Park on the oldest question past its deadline at the loop's
        next _maybe_park (a pending host park is folded into it)."""
        self._question_park_pending = True
        self.log(message, level="info", question_id=self._oldest_waiting())

    def _park_next_waiting(self) -> None:
        """Another question went past its deadline while the task was
        parked on the one just answered: park on that one next."""
        if self._waiting and self.settled is None and not self.parked and not self._question_park_pending:
            self._request_question_park("another question is unanswered: parking for it")

    def _expire_question(self, qid: str) -> None:
        """A question's deadline timer fired (or, on restore, its log-derived
        due time is behind): clock{question_deadline, ref} names it."""
        self.emit("clock", {"kind": "question_deadline", "at": self.clock.now_ms(), "ref": qid})
        self._question_expired(qid)

    def _question_expired(self, qid: str) -> None:
        """A question's deadline passed unanswered. answer_wait_s > 0:
        the task parks on it (the question stays open; an input-required
        mcp op stays suspended, no cancel); the park itself happens at the
        loop's next _maybe_park, never in the middle of a round. Otherwise the
        question is denied (three denials escalate)."""
        if self.limits.answer_wait_s <= 0:
            self._deny_question(qid)
            return
        if qid not in self.view.questions:
            return
        self._cancel(f"question:{qid}")
        self._waiting.add(qid)
        if self.parked:                      # cannot happen (timers pause): be safe
            return
        if self._question_park_pending:
            self.log("question deadline while parking on another question", level="info",
                     question_id=qid)
            return
        self._request_question_park("question unanswered: parking for the answer")

    def _deny_question(self, qid: str) -> None:
        pending = self.view.questions.get(qid)
        self._waiting.discard(qid)
        if pending is None:
            return
        self._cancel(f"question:{qid}")
        self.emit("guard_decision", {"ref": qid, "decision": "deny", "reason": UNANSWERED,
                                     "policy_id": "question"})            # TaskView closes it
        escalate = self.state.denied_questions >= MAX_UNANSWERED
        if pending["kind"] == "mcp_input":
            if escalate:
                self._mcp_cancel_input(pending["call_id"])
                self._abandon_open_work("killed")
                self._settle("escalated", "", reason="unanswered_questions")
                return
            # the gateway answers the cancel with the call's result
            self._mcp_resend(pending["call_id"], self._mcp_cancel(pending["call_id"]))
            return
        was = self._suppress_wake
        self._suppress_wake = was or escalate          # no further turn when escalating
        try:
            self._finish_call(pending["call_id"], "failed", "error: question unanswered before its deadline")
        finally:
            self._suppress_wake = was
        if escalate and self.settled is None:
            self._abandon_open_work("killed")
            self._settle("escalated", "", reason="unanswered_questions")

