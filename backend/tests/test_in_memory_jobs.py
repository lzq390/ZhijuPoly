from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.services.in_memory_jobs import (
    DEFAULT_JOB_RETENTION_SECONDS,
    DEFAULT_MAX_RETAINED_BYTES,
    DEFAULT_MAX_RETAINED_JOBS,
    BoundedInMemoryJobStore,
    JobGoneError,
    JobNotFoundError,
    JobStoreCapacityError,
)


@dataclass
class _Value:
    job_id: str
    payload: str = "small"


def _create(store: BoundedInMemoryJobStore, namespace: str = "polytao") -> _Value:
    return store.create(namespace, lambda job_id: _Value(job_id=job_id))


def test_default_retention_contract_is_24_hours_1000_jobs_and_256_mib() -> None:
    store = BoundedInMemoryJobStore(instance_id="a" * 16)

    assert store.retention_seconds == DEFAULT_JOB_RETENTION_SECONDS == 24 * 60 * 60
    assert store.max_jobs == DEFAULT_MAX_RETAINED_JOBS == 1000
    assert store.max_bytes == DEFAULT_MAX_RETAINED_BYTES == 256 * 1024 * 1024


def test_lookup_distinguishes_not_found_from_gone() -> None:
    store = BoundedInMemoryJobStore(instance_id="a" * 16)
    value = _create(store)

    with pytest.raises(JobNotFoundError):
        store.read("polytao", "not-a-job-id", lambda item: item)
    with pytest.raises(JobNotFoundError):
        store.read("conditional_generation", value.job_id, lambda item: item)
    with pytest.raises(JobGoneError):
        store.read(
            "polytao",
            "123e4567-e89b-42d3-a456-426614174000",
            lambda item: item,
        )
    with pytest.raises(JobGoneError):
        store.read(
            "polytao",
            uuid4().hex,
            lambda item: item,
        )
    # A random 32-character token is not a historical UUID4 job identifier.
    with pytest.raises(JobNotFoundError):
        store.read("polytao", "0" * 32, lambda item: item)

    previous_instance_id = value.job_id.replace("." + "a" * 16 + ".", "." + "b" * 16 + ".")
    with pytest.raises(JobGoneError):
        store.read("polytao", previous_instance_id, lambda item: item)

    store.delete("polytao", value.job_id)
    with pytest.raises(JobGoneError):
        store.read("polytao", value.job_id, lambda item: item)


def test_shared_limit_evicts_oldest_reapable_terminal_across_namespaces() -> None:
    now = [0.0]
    store = BoundedInMemoryJobStore(
        max_jobs=2,
        instance_id="a" * 16,
        monotonic_fn=lambda: now[0],
    )
    oldest = _create(store, "conditional_generation")
    store.mutate(
        "conditional_generation",
        oldest.job_id,
        lambda item: None,
        terminal=True,
    )
    store.mark_reapable("conditional_generation", oldest.job_id)
    now[0] = 1.0
    second = _create(store, "polytao")
    newest = _create(store, "conditional_generation")

    with pytest.raises(JobGoneError):
        store.read("conditional_generation", oldest.job_id, lambda item: item)
    assert store.read("polytao", second.job_id, lambda item: item.job_id) == second.job_id
    assert store.read("conditional_generation", newest.job_id, lambda item: item.job_id) == newest.job_id
    assert store.stats().jobs == 2


def test_active_or_not_yet_reapable_records_are_never_evicted() -> None:
    store = BoundedInMemoryJobStore(max_jobs=1, instance_id="a" * 16)
    active = _create(store)

    with pytest.raises(JobStoreCapacityError):
        _create(store, "conditional_generation")

    store.mutate("polytao", active.job_id, lambda item: None, terminal=True)
    with pytest.raises(JobStoreCapacityError):
        _create(store, "conditional_generation")

    store.mark_reapable("polytao", active.job_id)
    replacement = _create(store, "conditional_generation")
    assert replacement.job_id.startswith("conditional_generation.")


def test_terminal_ttl_starts_once_and_expires_only_after_future_is_reapable() -> None:
    now = [0.0]
    store = BoundedInMemoryJobStore(
        retention_seconds=10,
        instance_id="a" * 16,
        monotonic_fn=lambda: now[0],
    )
    value = _create(store)
    store.mutate("polytao", value.job_id, lambda item: None, terminal=True)
    now[0] = 9.0
    # A duplicate terminal transition must not extend the original TTL.
    store.mutate("polytao", value.job_id, lambda item: None, terminal=True)
    now[0] = 20.0
    assert store.read("polytao", value.job_id, lambda item: item.job_id) == value.job_id

    store.mark_reapable("polytao", value.job_id)
    with pytest.raises(JobGoneError):
        store.read("polytao", value.job_id, lambda item: item)


def test_oversized_mutation_rolls_back_the_retained_record() -> None:
    store = BoundedInMemoryJobStore(max_bytes=1024 * 1024, instance_id="a" * 16)
    value = _create(store)
    store.max_bytes = store.stats().bytes + 32

    with pytest.raises(JobStoreCapacityError):
        store.mutate(
            "polytao",
            value.job_id,
            lambda item: setattr(item, "payload", "x" * 16384),
            terminal=True,
        )

    retained = store.read("polytao", value.job_id, lambda item: item.payload)
    assert retained == "small"


def test_failed_oversized_mutation_does_not_evict_other_namespace_history() -> None:
    store = BoundedInMemoryJobStore(max_bytes=1024 * 1024, instance_id="a" * 16)
    history = _create(store, "conditional_generation")
    store.mutate(
        "conditional_generation",
        history.job_id,
        lambda item: setattr(item, "payload", "retained history"),
        terminal=True,
    )
    store.mark_reapable("conditional_generation", history.job_id)
    active = _create(store, "polytao")
    store.max_bytes = store.stats().bytes + 32

    with pytest.raises(JobStoreCapacityError):
        store.mutate(
            "polytao",
            active.job_id,
            lambda item: setattr(item, "payload", "x" * 16384),
            terminal=True,
        )

    assert store.read(
        "conditional_generation",
        history.job_id,
        lambda item: item.payload,
    ) == "retained history"
    assert store.read("polytao", active.job_id, lambda item: item.payload) == "small"
