"""Safe execution identifiers shared by adapters; never a task-state store."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging

logger = logging.getLogger("nexpoly.task_control")


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    owner_user_id: str
    request_id: str | None
    task_type: str
    channel: str
    task_id: str | None = None
    # Use an ordinal, chunk ID or Worker instance ID, never an execution secret.
    attempt_id: str | None = None


def log_task_event(context: TaskExecutionContext, event: str, *, reason: str | None = None) -> None:
    """Emit only the contract's identifiers and fixed event/reason codes.

    Do not pass Cookies, session/attempt tokens, inputs, result bodies or DSNs.
    The adapter emits authorization success only after its transaction commits.
    """
    logger.info(json.dumps({**asdict(context), "event": event, "reason": reason},
                           ensure_ascii=True, sort_keys=True, separators=(",", ":")))
