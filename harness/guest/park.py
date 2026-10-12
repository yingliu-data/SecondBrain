"""guest.park — park, resume and the settle, a mixin of
guest.coordinator.Coordinator (docs/GUEST.md "Park, resume and restore").

A park (the host's `park`, or a question past its deadline with
answer_wait_s > 0) waits until no provider call is in flight and no guest
operation runs (park_grace_s, then clock{grace_park} + ops.abort_all()); then
every timer pauses, checkpoint + parked{checkpoint_seq, reason, question_id?,
question_left_ms?, wall_used_ms?} and a bounded wait for the host's ack;
the connection that brings it goes idle at once (transport.park_on_ack; on
a timeout, transport.park()). The next link-up: clock{resumed}, the timers re-armed with
what they had left, the detached mcp ops re-attached.

The settle: settle{input_id, outcome, answer, usage (this task's), manifest}
— with a workspace, the manifest is guest.bulk.settle_manifest (changed files
and the manifest put on :5001 when there is a bulk client).
"""
from __future__ import annotations

from protocol.canonical import hash_value
from protocol.fold import checkpoint_body
from protocol.usage import normalise_usage
from guest.bulk import settle_manifest
from guest.taskview import RESUMED

PARK_ACK_WAIT_S = 60.0            # how long a park waits for the host's ack of `parked`
PARK_QUESTION = "question"        # parked.reason of a park on an unanswered question
PAUSED_ON_PARK = ("wall_clock", "question_deadline", "op_timeout", "idle_timer")


class FatalProtocolError(RuntimeError):
    """A protocol violation the guest cannot continue from (second hello)."""


def usage_delta(now: dict, base: dict) -> dict:
    p = now.get("prompt_tokens", 0) - base.get("prompt_tokens", 0)
    c = now.get("completion_tokens", 0) - base.get("completion_tokens", 0)
    return normalise_usage({"prompt_tokens": p, "completion_tokens": c})


class ParkMixin:
    _paused: dict
    _park_reason: str | None

    # ------------------------------------------------------------ timers
    def _pause_timers(self) -> None:
        """Park: cancel every timer, keeping what the ones that matter had
        left (the parked time does not count, as in a VM snapshot)."""
        now = self.clock.now_ms()
        kept = {}
        for token, (_, kind, due) in list(self._timers.items()):
            if kind in PAUSED_ON_PARK:
                kept[token] = (kind, max(0, due - now) / 1000.0)
            self._cancel(token)
        self._paused = kept

    def _resume_timers(self) -> None:
        paused, self._paused = self._paused, {}
        for token, (kind, remaining) in paused.items():
            self._arm(kind, token, remaining)

    # ------------------------------------------------------------ park
    def _park_wanted(self) -> bool:
        return self._park_reason is not None or self._question_park_pending

    def _guest_running(self) -> set:
        """Calls that run as guest operations (not host-side calls waiting for
        side_result, not questions waiting for an answer, not mcp calls: the
        forwarder holds those across a park)."""
        return self.view.guest_running(self._mcp_calls)

    def _request_park(self, reason: str) -> None:
        if self.parked or self._park_wanted():
            self.log("park requested while already parking; ignored", level="info", reason=reason)
            return
        self._park_reason = reason
        self.log("park requested", level="info", reason=reason)
        self._maybe_park()

    def _maybe_park(self) -> None:
        """Park when nothing guest-side is in flight; otherwise make sure the
        park grace runs while guest operations do."""
        if not self._park_wanted() or self.parked:
            return
        if self._guest_running():
            if "park_grace" not in self._timers:
                self._arm("grace_park", "park_grace", float(self.limits.park_grace_s))
            return
        if self.current_turn is not None:       # a provider call in flight: its turn ends first
            return
        self._park()

    def _on_grace_park(self) -> None:
        """The park grace ran out with guest operations still running: kill them."""
        running = self._guest_running()
        if not self._park_wanted() or not running:
            return
        self.emit("clock", {"kind": "grace_park", "at": self.clock.now_ms()})
        self.log("park grace expired: killing guest operations", level="warning",
                 running=sorted(running))
        self._detach_mcp()             # the forwarder keeps those; they are re-attached
        try:
            self.ops.abort_all()       # each reports state "killed" -> op_state{killed}
        except Exception as exc:    # noqa: BLE001
            self.log(f"abort_all failed: {exc}", level="warning")

    def _park(self) -> None:
        question = self._oldest_waiting() if self._question_park_pending and self._waiting else None
        reason = PARK_QUESTION if question is not None else self._park_reason
        self._park_reason, self._question_park_pending = None, False
        self._cancel("park_grace")
        self._detach_mcp()
        self._pause_timers()
        if self._session_start is None:
            raise FatalProtocolError("park without a session_start to chain the checkpoint to")
        prev = self._last_checkpoint_hash or hash_value(self._session_start)
        cp = self.emit("checkpoint", checkpoint_body([self._last_event], prev, state=self.state))
        body = {"checkpoint_seq": cp["seq"], "reason": reason}
        if question is not None:
            body["question_id"] = question
        # what each open question had left (restore re-arms it); 0 = due
        left = {qid: 0 for qid in self._waiting}
        left.update((token.split(":", 1)[1], int(round(remaining * 1000)))
                    for token, (kind, remaining) in self._paused.items() if kind == "question_deadline")
        if left:
            body["question_left_ms"] = left
        wall, paused = self._task_wall_s(), self._paused.get("wall")
        if wall and paused is not None:          # the task's wall clock used so far (restore reads it)
            body["wall_used_ms"] = max(0, int(round((float(wall) - paused[1]) * 1000)))
        # the link that brings the host's ack goes idle at once, in the
        # transport's own thread: the host snapshots right after its ack, and
        # a park() from here after a restore would close the restored link
        self.transport.park_on_ack(self.state.last_seq + 1)
        parked = self.emit("parked", body)
        self.parked = True
        self.log("parked", level="info", reason=reason, seq=parked["seq"], question_id=question)
        self.log_writer.flush(PARK_ACK_WAIT_S)
        if not self.transport.wait_acked(parked["seq"], PARK_ACK_WAIT_S):
            self.log(f"parked (seq {parked['seq']}) not acked within {PARK_ACK_WAIT_S:g}s; "
                     f"going idle anyway (it is resent on reconnect)", level="warning")
            self.transport.park()
        self._on_ack(self.transport.acked_seq)
        self.log_writer.sync_acked()             # the ack marker on disk (the link is idle already)

    def _unpark(self) -> None:
        self.parked = False
        self.emit("clock", {"kind": RESUMED, "at": self.clock.now_ms()})
        self.log("resumed after park", level="info")
        self._resume_timers()
        self._reattach_mcp()

    # ------------------------------------------------------------ settle
    def _settle(self, outcome: str, answer: str, **extra) -> None:
        if self.settled is not None:
            return
        self._cancel_all()
        self._waiting.clear()
        self._question_park_pending = False
        body = {
            "input_id": self.view.task_input_id or "",
            "outcome": outcome,
            "answer": answer,
            "usage": usage_delta(self.state.usage, self.view.usage_base),
            "manifest": self._settle_manifest(),
            **extra,
        }
        self.emit("settle", body)
        self.settled = body

    def _settle_manifest(self) -> dict:
        """The workspace manifest; changed files and the
        manifest go to the host on :5001 first (when there is a client)."""
        if self.workspace is None:
            return {}
        try:
            return settle_manifest(self.workspace, self.bulk, self._uploaded, self._digests)
        except Exception as exc:    # noqa: BLE001 - a manifest problem must not block the settle
            self.log(f"workspace manifest failed: {exc}", level="warning")
            return {}
