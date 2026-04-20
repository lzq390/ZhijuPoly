from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class InvalidSmilesError(ServiceError):
    """Raised when the input SMILES cannot be parsed by RDKit."""


class UnsupportedPredictionPropertyError(ServiceError):
    """Raised when a requested prediction property is not supported."""


class ModelArtifactError(ServiceError):
    """Raised when a model artifact is missing or incompatible."""
