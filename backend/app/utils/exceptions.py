from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class InvalidSmilesError(ServiceError):
    """Raised when the input SMILES cannot be parsed by RDKit."""
