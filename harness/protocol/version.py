"""version — the harness release both halves are built from.

The host sends it in `hello.config.harness_version`; the guest records its own
in `session_start.harness_version`; the host logs a `harness_skew` warning host
event when the two differ (a guest image built from other code: warn only).
"""
HARNESS_VERSION = "0.0.3"
