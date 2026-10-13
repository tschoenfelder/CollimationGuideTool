"""The exceptions an OnStep operation through OnStepAdapter may raise (#52 K02; S6.6b).

Neutral module -- no OnStepAdapter import -- so every OnStep shim and any caller that has to
classify an OnStep failure reads the one set from here.
"""

from __future__ import annotations

#: #52 K02 (S6.3): THE set of exceptions an OnStep operation through OnStepAdapter may raise
#: for a device-side reason -- every OnStep shim (`OnStepMountPulseAdapter`,
#: `OnStepFocuserAdapter`) turns exactly these into a result/refusal; anything else (a
#: TypeError, AttributeError...) is a bug and propagates. OSError covers
#: ConnectionError/TimeoutError and a raw socket error; KeyError is OnStepAdapter's reply
#: missing a field (e.g. 'UTC'). The connect path is deliberately broader
#: (`OnStepConnection.acquire` closes on ANY failure, S6.2).
ONSTEP_OPERATION_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
)

__all__ = ["ONSTEP_OPERATION_ERRORS"]
