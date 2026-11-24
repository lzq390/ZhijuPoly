"""Fixed loopback candidate profiles used by the independent OpenScience authorities."""

import json
import os
from pathlib import Path


PROFILES = json.loads(Path(__file__).with_name("openscience_candidates.json").read_bytes())


def authority_environment():
    name = os.environ.get("OPENSCIENCE_M2_ENVIRONMENT", "dev-m1")
    if name not in PROFILES:
        raise ValueError("Unknown independent OpenScience development environment")
    return name
