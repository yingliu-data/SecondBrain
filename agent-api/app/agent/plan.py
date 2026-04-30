"""Plan / Step dataclasses — the serialized artefact of a planner turn.

Phase 2 treats plans as advisory: they're emitted to the client and injected
into the system prompt, but execution still runs through the single-loop.
Phase 3's orchestrator uses ``state``/``depends_on``/``mode`` to drive actual
step-by-step (and eventually parallel, worker-backed) execution.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Step:
    id: str
    goal: str
    mode: str                                     # "inline" | "worker"
    suggested_tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    state: str = "pending"                        # pending | running | done | failed
    worker_id: str | None = None
    result_summary: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    plan_id: str
    steps: list[Step]
    path: Path

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.to_dict(), indent=2), encoding="utf-8"
        )

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "steps": [s.to_dict() for s in self.steps],
        }

    def ready_steps(self) -> list[Step]:
        """Pending steps whose dependencies are all done — used by Phase 3."""
        done = {s.id for s in self.steps if s.state == "done"}
        return [
            s for s in self.steps
            if s.state == "pending" and all(d in done for d in s.depends_on)
        ]

    def all_terminal(self) -> bool:
        return all(s.state in ("done", "failed") for s in self.steps)

    def as_prompt_block(self) -> str:
        """Compact plan view injected into the system prompt."""
        lines = ["PLAN (follow these steps; parallel where deps allow):"]
        for s in self.steps:
            deps = f" ← {','.join(s.depends_on)}" if s.depends_on else ""
            tools = (
                f" [tools: {', '.join(s.suggested_tools)}]"
                if s.suggested_tools else ""
            )
            lines.append(f"- [{s.id}] {s.goal}{deps}{tools}")
        return "\n".join(lines)
