"""A small, generic INDI protocol client — see `client.py`'s docstring.

Kept separate from `focus/` (rather than folded into a focuser-specific
module) since the wire protocol itself has nothing to do with focusers —
an INDI-based mount adapter, if ever wanted, would reuse this unchanged.
"""

from astrotool_core.indi.client import IndiClient, VectorState

__all__ = ["IndiClient", "VectorState"]
