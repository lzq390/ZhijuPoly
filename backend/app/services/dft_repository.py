from __future__ import annotations

import ast
import json
from typing import Any


def parse_coordinates(coordinates: str) -> list[tuple[int, float, float, float]]:
    try:
        parsed: Any = json.loads(coordinates)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(coordinates)

    return [
        (int(atom[0]), float(atom[1]), float(atom[2]), float(atom[3]))
        for atom in parsed
    ]