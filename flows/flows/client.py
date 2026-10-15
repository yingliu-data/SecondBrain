"""Client-side helpers agent-api's routes call (``POST /chat``, ``answer_to``, status).

These need a connected ``temporalio.client.Client``; they are not workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy

from flows import TASK_QUEUE
from flows.activities import SessionConfig, SessionRef, TaskSignal
from flows.session_workflow import (
    AbortSignal,
    AnswerSignal,
    SessionState,
    SessionWorkflow,
    Status,
    workflow_id,
)


async def signal_task(
    client: Client,
    session: SessionRef,
    task: TaskSignal,
    *,
    task_queue: str = TASK_QUEUE,
    config: SessionConfig | None = None,
    tenant: dict[str, Any] | None = None,
) -> WorkflowHandle:
    """``signal_with_start``: start the session workflow if needed, deliver the task.

    Idempotent per ``task.input_id``: the workflow drops duplicates.  The
    config (explicit, or ``SessionConfig.from_tenant(tenant)``) only applies
    when this call starts the workflow.
    """
    if config is None:
        config = SessionConfig.from_tenant(tenant) if tenant else SessionConfig()
    return await client.start_workflow(
        SessionWorkflow.run,
        SessionState(session=session, config=config),
        id=workflow_id(session.user, session.tenant, session.session_id),
        task_queue=task_queue,
        start_signal="task",
        start_signal_args=[task],
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


def session_handle(client: Client, session: SessionRef) -> WorkflowHandle:
    return client.get_workflow_handle_for(
        SessionWorkflow.run,
        workflow_id(session.user, session.tenant, session.session_id),
    )


async def signal_answer(client: Client, session: SessionRef, answer: AnswerSignal) -> None:
    await session_handle(client, session).signal(SessionWorkflow.answer, answer)


async def signal_abort(client: Client, session: SessionRef, reason: str) -> None:
    await session_handle(client, session).signal(SessionWorkflow.abort, AbortSignal(reason=reason))


async def query_status(client: Client, session: SessionRef) -> Status:
    return await session_handle(client, session).query(SessionWorkflow.status)
