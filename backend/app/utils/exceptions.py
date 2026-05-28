from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class InvalidSmilesError(ServiceError):
    """Raised when the input SMILES cannot be parsed by RDKit."""


class UnsupportedPredictionPropertyError(ServiceError):
    """Raised when a requested prediction property is not supported."""


class ModelArtifactError(ServiceError):
    """Raised when a model artifact is missing or incompatible."""


class InvalidImageError(ServiceError):
    """Raised when an uploaded image cannot be used for recognition."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class StructureRecognitionError(ServiceError):
    """Raised when image recognition does not produce a usable structure."""
