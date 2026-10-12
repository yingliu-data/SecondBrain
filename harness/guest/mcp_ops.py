"""guest.mcp_ops — the mcp op lifecycle of the coordinator (docs/GUEST.md
"MCP calls"): start, input required, re-send, timeout, detach on a park,
re-attach on the resume. A mixin of guest.coordinator.Coordinator; it owns
`_mcp_calls`, the one piece of mcp bookkeeping the log does not hold (the
POST in flight and the op timer).

_mcp_calls[call_id] = {args, attempt, state, timer_ms, input_response, started_ms}:
  attempt   the POST in flight or last sent (rpc id <call_id>#<attempt>)
  state     "inflight" (a POST runs in ops), "detached" (park: the forwarder
            holds it) or "asked" (suspended on an input request)
  timer_ms  the op timeout: the absolute due time (ms) while inflight or
            detached, the time left (ms) while asked; None = no timeout
The name and the input request come from state.open_calls.
"""
from __future__ import annotations

from protocol.events import OP_INPUT_REQUIRED
from protocol.mcp import CALL_TOOL, split_namespaced
from guest.mcp import input_response, question_options


def gateway_ref(name: str, args: dict) -> str:
    """op_state.side_ref of an mcp call: gateway:<upstream> (the target of
    call_tool, the prefix of a promoted upstream__tool), else gateway."""
    target = args.get("name") if name == CALL_TOOL and isinstance(args.get("name"), str) else name
    upstream, tool = split_namespaced(target)
    return f"gateway:{upstream}" if tool else "gateway"


class McpOpsMixin:
    _mcp_calls: dict

    def _new_mcp(self, call_id: str, name: str, args: dict, *, attempt: int = 0,
                 state: str = "asked") -> dict:
        timeout = self.cfg.op_timeout_for(name, "mcp")
        mcp = {"args": args, "attempt": attempt, "state": state,
               "timer_ms": int(timeout * 1000) if timeout else None,
               "input_response": None, "started_ms": self.clock.now_ms()}
        self._mcp_calls[call_id] = mcp
        return mcp

    def _start_mcp(self, call_id: str, name: str, args: dict) -> None:
        self.emit("op_state", {"call_id": call_id, "state": "running",
                               "side_ref": gateway_ref(name, args)})
        self._new_mcp(call_id, name, args)
        self._mcp_post(call_id)

    def _mcp_resend(self, call_id: str, response: dict) -> None:
        """The suspended call re-sent with the user's answer (or a cancel):
        op_state{running, attempt + 1}, then the POST."""
        mcp = self._mcp_calls[call_id]
        mcp["attempt"] += 1
        mcp["input_response"] = response
        self.emit("op_state", {"call_id": call_id, "state": "running", "attempt": mcp["attempt"]})
        self._mcp_post(call_id)

    def _mcp_answer(self, call_id: str, answer: str) -> None:
        """A ui_response to the call's input request: the re-send with it."""
        request = self.state.open_calls[call_id]["input_request"]
        self._mcp_resend(call_id, input_response(request["request_state"], answer, request["schema"]))

    def _mcp_post(self, call_id: str) -> None:
        """One POST of the call: from "asked" (the first, or a re-send: the
        time the op had left is armed again) or from "detached" (a re-attach
        after a park: the same request, the op timer's due time unchanged)."""
        mcp = self._mcp_calls[call_id]
        now = self.clock.now_ms()
        reattach = mcp["state"] == "detached"
        if mcp["state"] == "asked" and mcp["timer_ms"] is not None:
            mcp["timer_ms"] += now                       # time left -> due time
        mcp["state"] = "inflight"
        remaining = None
        if mcp["timer_ms"] is not None:
            remaining = max(0.001, (mcp["timer_ms"] - now) / 1000.0)
            self._arm("mcp_timeout", f"op:{call_id}", remaining)
        try:
            # the socket timeout is only a backstop behind the op timer
            self.ops.submit_mcp(call_id, self.state.open_calls[call_id]["name"], mcp["args"],
                                attempt=mcp["attempt"], input_response=mcp["input_response"],
                                reattach=reattach, timeout_s=(remaining + 5.0) if remaining else None)
        except Exception as exc:    # noqa: BLE001
            self._finish_call(call_id, "failed", f"error: could not call the gateway: {exc}")

    def _mcp_timeout(self, call_id: str) -> None:
        mcp = self._mcp_calls[call_id]
        name = self.state.open_calls[call_id]["name"]
        try:
            self.ops.cancel(call_id)
        except Exception as exc:    # noqa: BLE001
            self.log(f"ops.cancel failed: {exc}", level="warning")
        self._finish_call(call_id, "timeout",
                          f"error: mcp call {name} timed out after {self.cfg.op_timeout_for(name, 'mcp'):g} s",
                          duration_ms=max(0, self.clock.now_ms() - mcp["started_ms"]))

    def _on_op_input(self, call_id: str, request: dict) -> None:
        """The gateway needs input: suspend the op and ask."""
        mcp = self._mcp_calls.get(call_id)
        call = self.state.open_calls.get(call_id)
        if mcp is None or call is None or "result" in call or call_id not in self.view.turn_pending:
            self.log(f"input request for unknown or terminal call {call_id!r} ignored", level="warning")
            return
        self._cancel(f"op:{call_id}")
        if mcp["timer_ms"] is not None:
            mcp["timer_ms"] = max(1000, mcp["timer_ms"] - self.clock.now_ms())   # due time -> time left
        mcp["state"] = "asked"
        schema = request.get("schema") or {}
        self.emit("op_state", {"call_id": call_id, "state": OP_INPUT_REQUIRED, "attempt": mcp["attempt"],
                               "input_request": {"request_state": request["request_state"],
                                                 "schema": schema}})
        self._ask(call_id, text=str(request.get("message") or ""), options=question_options(schema),
                  timeout_s=self._question_timeout(request.get("expires_in_s")))

    def _mcp_cancel(self, call_id: str) -> dict:
        """The input response that cancels a suspended call's request."""
        request = self.state.open_calls[call_id]["input_request"]
        return input_response(request["request_state"], "cancel", {})

    def _mcp_cancel_input(self, call_id: str) -> None:
        """Best effort: tell the gateway a suspended call is abandoned."""
        mcp = self._mcp_calls.get(call_id)
        if mcp is None:
            return
        try:
            self.ops.send_mcp(call_id, self.state.open_calls[call_id]["name"], mcp["args"],
                              attempt=mcp["attempt"] + 1, input_response=self._mcp_cancel(call_id))
        except Exception as exc:    # noqa: BLE001
            self.log(f"mcp cancel re-send failed: {exc}", level="warning")

    def _detach_mcp(self) -> None:
        """Park: close every in-flight mcp connection without cancelling the
        op (the forwarder keeps the call)."""
        try:
            detached = self.ops.detach_mcp()
        except Exception as exc:    # noqa: BLE001
            self.log(f"detach_mcp failed: {exc}", level="warning")
            return
        detached = sorted(cid for cid in detached if cid in self._mcp_calls)
        for cid in detached:
            self._mcp_calls[cid]["state"] = "detached"
        if detached:
            self.log("mcp calls detached for the park", level="info", calls=detached)

    def _reattach_mcp(self) -> None:
        """Resume: an expired detached op is a timeout; the others re-attach."""
        now = self.clock.now_ms()
        for cid in [c for c, m in self._mcp_calls.items() if m["state"] == "detached"]:
            due = self._mcp_calls[cid]["timer_ms"]
            if due is not None and now >= due:
                self._mcp_timeout(cid)
            else:
                self._mcp_post(cid)

    def _restore_mcp(self) -> None:
        """restore(): the mcp ops of the folded state. running = a POST was in
        flight (lost unless a park detached it), input_required = asked."""
        for call_id, call in self.state.open_calls.items():
            if call["side"] == "mcp" and "result" not in call \
                    and call["state"] in ("running", OP_INPUT_REQUIRED):
                self._new_mcp(call_id, call["name"], self.view.calls[call_id]["args"],
                              attempt=call.get("attempt", 0),
                              state="inflight" if call["state"] == "running" else "asked")
