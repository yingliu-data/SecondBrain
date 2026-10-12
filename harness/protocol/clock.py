"""clock — wall-clock and elapsed-time helpers. Stdlib only.

  now_ms()                 epoch milliseconds (int)
  now_iso()                UTC "YYYY-MM-DDTHH:MM:SS.mmmZ": the event `ts`
                           format (guest events, host-log records, session.json)
  elapsed_ms(t0, digits=None)
                           milliseconds since `t0` (a time.monotonic() value):
                           an int, or rounded to `digits` decimals when given

Pure helpers: the fold never calls them (event times are data).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def elapsed_ms(t0: float, digits: int | None = None):
    ms = (time.monotonic() - t0) * 1000
    return int(ms) if digits is None else round(ms, digits)
