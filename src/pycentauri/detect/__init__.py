"""Failed-print ("spaghetti") detection: Coral Edge TPU / CPU TFLite.

Requires the ``detect`` extra (``pip install 'pycentauri[detect]'``).
Symbols resolve lazily so importing this package on a base install
(without ``ai-edge-litert``/``numpy``/``pillow``) stays harmless; the
actual model load happens in :mod:`pycentauri.detect.backend`.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DetectBackendError",
    "DetectConfig",
    "Detection",
    "DetectionController",
    "DetectionEvent",
    "Detector",
]

_LAZY_MODULES = {
    "DetectBackendError": "backend",
    "Detection": "backend",
    "Detector": "backend",
    "DetectConfig": "pipeline",
    "DetectionController": "pipeline",
    "DetectionEvent": "pipeline",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, name)
