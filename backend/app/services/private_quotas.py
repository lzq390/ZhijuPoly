"""Server-only limits for private retention and model input/output."""
from dataclasses import dataclass, fields
import os


@dataclass(frozen=True, slots=True)
class PrivateQuotaSettings:
    memory_jobs_per_user: int = 100
    memory_bytes_per_user: int = 64 * 1024 * 1024
    memory_retention_seconds: int = 24 * 60 * 60
    browsing_recordings_per_user: int = 8
    browsing_snapshots_per_user: int = 16
    browsing_bytes_per_user: int = 16 * 1024 * 1024
    browsing_record_bytes: int = 4 * 1024 * 1024
    browsing_idle_seconds: int = 3600
    summary_output_bytes: int = 1024 * 1024
    summary_evidence_characters: int = 80_000
    summary_max_tokens: int = 4096
    online_history_max_rows: int = 1000
    online_history_max_bytes: int = 64 * 1024 * 1024
    online_job_max_rows: int = 1000
    online_job_max_bytes: int = 128 * 1024 * 1024
    online_result_bytes: int = 8 * 1024 * 1024
    online_execution_seconds: int = 600
    reverse_execution_seconds: int = 600
    prediction_smiles_characters: int = 8000
    model_input_characters: int = 80_000
    model_output_bytes: int = 1024 * 1024
    model_execution_seconds: int = 600

    @classmethod
    def from_environment(cls):
        values = {}
        for field in fields(cls):
            key = 'PRIVATE_' + field.name.upper()
            raw = os.getenv(key)
            if raw is not None:
                try:
                    values[field.name] = int(raw)
                except ValueError:
                    raise ValueError(f'{key} must be a positive integer') from None
                if values[field.name] < 1:
                    raise ValueError(f'{key} must be a positive integer')
        return cls(**values)
