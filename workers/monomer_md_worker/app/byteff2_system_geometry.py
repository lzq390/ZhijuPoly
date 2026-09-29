"""Physical periodic-box validation independent of OpenMM/GPU initialization."""
from __future__ import annotations

import math


class FormalSystemSizeError(ValueError):
    pass


def validate_periodic_box(vectors, cutoff_nm: float) -> dict:
    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

    if len(vectors) != 3 or any(len(v) != 3 for v in vectors):
        raise FormalSystemSizeError("Invalid periodic box vectors")
    if not all(math.isfinite(float(x)) for v in vectors for x in v) or not math.isfinite(cutoff_nm) or cutoff_nm <= 0:
        raise FormalSystemSizeError("Invalid periodic box or cutoff")
    a, b, c = vectors
    volume = abs(sum(x*y for x, y in zip(a, cross(b, c))))
    areas = [math.sqrt(sum(x*x for x in cross(u, v))) for u, v in ((b, c), (a, c), (a, b))]
    if not math.isfinite(volume) or not all(math.isfinite(area) for area in areas) or volume <= 0 or min(areas) <= 0:
        raise FormalSystemSizeError("Degenerate periodic box")
    heights = [volume / area for area in areas]
    if min(heights) < 2 * cutoff_nm:
        raise FormalSystemSizeError(
            f"Formal periodic system is too small: minimum box height {min(heights):.4f} nm "
            f"is below twice the cutoff ({2 * cutoff_nm:.4f} nm); increase the system size (natoms)."
        )
    return {"box_heights_nm": heights, "cutoff_nm": cutoff_nm}
