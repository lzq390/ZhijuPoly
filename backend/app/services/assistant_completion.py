"""Server-only completion evidence; never accept qualification facts from HTTP."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from fastapi import HTTPException


_current: ContextVar[CompletionEvidence | None] = ContextVar("assistant_completion_evidence", default=None)


def current_completion_evidence():
    return _current.get()


@contextmanager
def completion_scope(evidence):
    token = _current.set(evidence)
    try:
        yield evidence
    finally:
        _current.reset(token)


@dataclass
class CompletionEvidence:
    provider_attempt_limit: int
    max_output_tokens: int
    attempts: int = 0
    outcome: str = "partial"
    validated: bool = False
    local_result_proven: bool = False
    _phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    _records: list[dict[str, Any]] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def begin_provider(self, phase, kwargs=None):
        with self._lock:
            if self.attempts >= self.provider_attempt_limit:
                raise HTTPException(429, "Assistant provider attempt budget exhausted")
            if kwargs is not None:
                cap = next((kwargs[key] for key in ("max_output_tokens", "max_completion_tokens", "max_tokens") if key in kwargs), None)
                if type(cap) is not int or not 1 <= cap <= self.max_output_tokens:
                    raise HTTPException(422, "Assistant provider output budget is invalid")
            self.attempts += 1
            record = {"attempt": self.attempts, "completed": False, "refusal": False, "terminal": False}
            self._phases[phase] = record
            self._records.append(record)
            return record

    def observe_provider(self, record, *, completed=False, refusal=False, terminal=False):
        with self._lock:
            record["completed"] = record["completed"] or bool(completed)
            record["refusal"] = record["refusal"] or bool(refusal)
            record["terminal"] = record["terminal"] or bool(terminal or completed)

    def all_providers_terminated(self):
        with self._lock:
            return all(record["terminal"] for record in self._records)

    def mark_business(self, outcome, *, validated=False, local_result=False):
        with self._lock:
            self.outcome, self.validated = outcome, bool(validated)
            self.local_result_proven = bool(local_result)

    def facts(self, output_present):
        with self._lock:
            final = self._phases.get("summary") or self._phases.get("answer")
            refusal = bool(final and final["refusal"])
            proven = (self.local_result_proven or (self.outcome == "clarification" and self.validated)
                      or bool(final and ((final["completed"] and not refusal) or (refusal and final["terminal"]))))
            return {"validated": self.validated, "completion_proven": proven,
                    "output_present": bool(output_present),
                    "outcome": "refusal" if refusal and not self.local_result_proven else self.outcome}


def mark_assistant_result(outcome="service_completed", *, validated=True, local_result=False):
    evidence = current_completion_evidence()
    if evidence is not None:
        evidence.mark_business(outcome, validated=validated, local_result=local_result)
