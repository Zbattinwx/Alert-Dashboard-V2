"""
Small pieces of mutable state shared between route groups.

`_current_radar_product` lived at module scope in main.py and was mutated with
`global` from a handler. Two route groups read it, so when those groups moved
into routers the `global` declaration went with them -- and a `global` statement
does not create the name, it only says which scope to bind in. The handler
therefore raised NameError on the read, while every import succeeded and the
route registered normally.

Accessors instead of a bare module variable, so the value can only be reached
through a function call. `from ..runtime_state import _current_radar_product`
would copy the value at import time and silently never see an update, which is
the same class of bug wearing different clothes.
"""

from __future__ import annotations

# Which radar product the stream overlay is currently showing. Set from the
# stream endpoint, read by the map view.
_radar_product: str = "reflectivity"


def get_radar_product() -> str:
    return _radar_product


def set_radar_product(value: str) -> str:
    """Set and return the normalised product. Ignores blank input."""
    global _radar_product
    if value and value.strip():
        _radar_product = value.strip().lower()
    return _radar_product
