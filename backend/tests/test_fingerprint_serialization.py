from __future__ import annotations

from app.services.fingerprint import fingerprint_from_bytes, fingerprint_to_bytes, generate, tanimoto


def test_fingerprint_serialization_round_trip() -> None:
    fp = generate("CCO")

    data = fingerprint_to_bytes(fp)
    restored = fingerprint_from_bytes(data)

    assert isinstance(data, bytes)
    assert len(data) == 256
    assert tanimoto(fp, restored) == 1.0
