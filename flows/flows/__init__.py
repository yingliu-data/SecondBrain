"""SecondBrain Temporal control plane."""

TASK_QUEUE = "secondbrain-agent"
"""The one task queue: session workflows, their activities, schedule ticks."""

NAMESPACE = "secondbrain"
"""The Temporal namespace when TEMPORAL_NAMESPACE is unset (agent-api's default too)."""
