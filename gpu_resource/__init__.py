"""Shared, dependency-free GPU broker client primitives."""

from .client import (
    GpuBrokerClient,
    GpuBrokerClientError,
    GpuLease,
    ManagedGpuLease,
    mps_client_environment,
)
from .transient_scope import (
    SCOPE_SLICE,
    TransientScopeError,
    scope_control_group,
    scope_unit_name,
    transient_scope_command,
    user_manager_control_group,
    validate_lease_id,
    wait_for_scope_membership,
)

__all__ = [
    "GpuBrokerClient",
    "GpuBrokerClientError",
    "GpuLease",
    "ManagedGpuLease",
    "mps_client_environment",
    "SCOPE_SLICE",
    "TransientScopeError",
    "scope_control_group",
    "scope_unit_name",
    "transient_scope_command",
    "user_manager_control_group",
    "validate_lease_id",
    "wait_for_scope_membership",
]
