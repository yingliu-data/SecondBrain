"""adopted — device calls a new Worker takes over from a parked session's
log (an agent-api restart; docs/HOST.md "Across an agent-api restart").

  AdoptedCalls(device_calls, ref, send=, host_event=, timeout_s=, label=)
    .adopt(state, loop)        every device call the fold has open (tool_call
                               side device, no result; its args from the fold's
                               call_args index) registered in DeviceCalls with
                               a deliver callback, so a POST /tool-result after
                               the restart is accepted and a re-attaching
                               stream re-emits it
    .deliver(call_id, name, result)   side_result to the guest through `send`
                               (the Worker's send_control: held while parked,
                               the guest wakes for it). Raises GuestError when
                               no guest run takes it: DeviceCalls keeps the
                               call open and the device may POST again. Once
                               handed over the call_id is remembered: a second
                               delivery is not sent again
    .release_all()             a closing Worker: its calls leave DeviceCalls
                               (a later POST is a 409), unless a newer Worker
                               adopted them meanwhile
    len(), `in`                the calls still waiting for their result

No Sides timer waits on an adopted call: it gets the same limit as a live
one (timeout_s, the device tool timeout), counted from the adoption (the
parked guest's own timers do not run until something wakes it).
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Callable

from protocol.fold import State

from app.host.outcomes import GuestError
from app.host.sides import DeviceCalls, device_content, device_timeout

logger = logging.getLogger("host.adopted")


class AdoptedCalls:
    def __init__(self, device_calls: DeviceCalls, ref, *,
                 send: Callable[[str, dict], str],
                 host_event: Callable[[str, dict], object],
                 timeout_s: float, closed: Callable[[], bool] = lambda: False,
                 label: str = "") -> None:
        self.device_calls = device_calls
        self.ref = ref
        self.send = send
        self.host_event = host_event
        self.timeout_s = timeout_s
        self.closed = closed
        self.label = label
        self._waiting: dict[str, Callable[[str], None]] = {}   # call_id -> its deliver
        self._delivered: set[str] = set()

    def __len__(self) -> int:
        return len(self._waiting)

    def __contains__(self, call_id) -> bool:
        return call_id in self._waiting

    def adopt(self, state: State, loop: asyncio.AbstractEventLoop | None) -> None:
        for cid, call in state.open_calls.items():
            if call["side"] != "device" or "result" in call:
                continue
            name = call["name"]
            deliver = functools.partial(self.deliver, cid, name)
            self._waiting[cid] = deliver
            self.device_calls.adopt(cid, name, state.call_args.get(cid, {}), self.ref,
                                    loop=loop, deliver=deliver)
            if loop is not None:
                loop.call_later(self.timeout_s, self._timeout, cid, name)
            self.host_event("side_call", {"side": "device", "call_id": cid,
                                          "name": name, "state": "adopted"})

    def watch(self, call_id: str, deliver: Callable[[str], None]) -> None:
        """Track a call adopted elsewhere (tests, a hand-made adoption)."""
        self._waiting[call_id] = deliver

    def _timeout(self, call_id: str, name: str) -> None:
        if call_id in self._waiting and not self.closed():
            self.device_calls.resolve(call_id, device_timeout(name, self.timeout_s))

    def deliver(self, call_id: str, name: str, result: str) -> None:
        if call_id in self._delivered:
            return
        if self.closed():
            raise GuestError("worker closed")
        content, ok = device_content(result)
        status = self.send("side_result", {"call_id": call_id, "side": "device",
                                           "content": content, "ok": ok})
        if status == "no_guest":
            raise GuestError("no guest run takes the result")
        self._delivered.add(call_id)
        self._waiting.pop(call_id, None)
        try:
            self.host_event("side_call", {"side": "device", "call_id": call_id,
                                          "name": name, "ok": ok, "adopted": True,
                                          "sent": status})
        except Exception:       # delivered: the trace never makes it a failure
            logger.exception("session %s: side_call host event", self.label)

    def release_all(self) -> None:
        for cid, deliver in self._waiting.items():
            self.device_calls.release(cid, deliver)     # a later POST is a 409
        self._waiting.clear()
