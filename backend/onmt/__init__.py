from __future__ import annotations

from pkgutil import extend_path

# OpenNMT-py 2.2 imports legacy torchtext modules from its package entrypoint.
# MolScribe only needs OpenNMT's decoder/modules subpackages, so this local shim
# keeps those imports usable in the screen312 Python 3.12 environment.
__path__ = extend_path(__path__, __name__)
__version__ = "2.2.0"
