"""OnStepConnection — the one owner of this app's single `OnStepIndiClient`.

OnStepAdapter >= 0.4.0 talks to OnStep over INDI (AGENTS.md: for this
deployment, indiserver is the sole owner of the OnStep serial port, and
OnStepAdapter itself must use the INDI-backed transport, so IndiMonitor and
other INDI clients keep receiving mount/focuser properties). The focuser,
park and pulse shims must still share ONE client rather than each opening
their own connection to the same INDI device; each shim `acquire()`s on
`connect()` and `release()`s on `disconnect()` -- the connection is opened
by the first and closed by the last, exactly as it was for 0.3.5's serial
client (that lifecycle policy is orthogonal to which transport backs it).

Real field report (diagnostic 7b21bdf1, 2026-09-29): a focuser move kept
getting rejected in the live app but succeeded every time in an isolated,
single-threaded reproduction script talking to the same live controller.
The difference: `AutofocusRunner.submit()` runs a move on a real
background `threading.Thread` while `FocuserPanel`/`MountTestMovePanel`/
`MountParkPanel` each poll `status()` on a `QTimer` (the GUI thread) every
~250ms -- all sharing this one connection. `IndiTransport`'s own property
dict is internally thread-safe (a `threading.Condition` guards it), but
nothing serializes a whole *compound* operation (e.g. `move_absolute()`'s
read-check-send-wait sequence) against a concurrent, unrelated `status()`
call arriving mid-sequence from another thread -- exactly the standard
shared-hardware-resource-across-threads hazard. `operation_lock` below is
the standard fix: callers wrap each of their own public operations in it
so the GUI-thread poll and a worker-thread move can never interleave.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from onstep_adapter import IndiRuntimeConfig, OnStepIndiClient


class OnStepConnection:
    def __init__(
        self,
        config: IndiRuntimeConfig,
        *,
        timeout: float = 5.0,
        client_factory: Callable[..., OnStepIndiClient] = OnStepIndiClient,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._client_factory = client_factory
        self._client: OnStepIndiClient | None = None
        self._users = 0
        self._lock = threading.Lock()
        #: Serializes whole operations (status()/move_absolute()/etc.)
        #: across every adapter sharing this connection -- see this
        #: module's own docstring. RLock: a caller's own operation may
        #: legitimately call back into another of its own locked methods
        #: on the same thread (e.g. move() reading status() first).
        self.operation_lock = threading.RLock()

    @contextmanager
    def try_operation(self, wait_s: float = 0.0) -> Iterator[bool]:
        """Enter `operation_lock` only if that does not mean waiting longer than `wait_s`
        (S6.0c; default: not at all).

        Yields True while holding it (re-entrant like the lock), False -- without
        touching the lock -- when another thread's operation holds it right now.
        For a GUI-thread status READ only: an OnStepAdapter axis GOTO holds the
        lock for its whole blocking duration (up to its 30 s timeout), and a
        poll that queues behind it freezes the Qt event loop. A caller that gets
        False must not talk to the controller (that would be exactly the
        interleaving 9cea2e9 forbids) -- it serves what it last read. Compound
        operations keep using `with operation_lock` and still serialize.

        `wait_s > 0` is for a DECISION read off the GUI thread (re-review R1): GUI polls
        hold the lock for a moment on every read, so a decision waits that long for a
        fresh reading instead of failing on a mere collision -- bounded, never "until the
        GOTO ends". A lock wait, so it runs on real time, not the injected clock."""
        if wait_s > 0:
            entered = self.operation_lock.acquire(timeout=wait_s)
        else:
            entered = self.operation_lock.acquire(blocking=False)
        try:
            yield entered
        finally:
            if entered:
                self.operation_lock.release()

    @property
    def config(self) -> IndiRuntimeConfig:
        return self._config

    @property
    def client(self) -> OnStepIndiClient | None:
        """The connected client, or None while nobody holds the connection."""
        return self._client

    def acquire(self) -> OnStepIndiClient:
        """Open (first user) or share (later users) the client. Raises
        `ConnectionError` (or whatever `connect()` itself raises) if the
        controller cannot be reached; a failed open leaves no half-open
        state and no user count behind."""
        with self._lock:
            if self._client is None:
                client = self._client_factory(config=self._config)
                try:
                    client.connect(timeout=self._timeout)
                except (ConnectionError, RuntimeError, TimeoutError, ValueError):
                    client.close()
                    raise
                self._client = client
            self._users += 1
            return self._client

    def release(self) -> None:
        with self._lock:
            if self._users == 0:
                return
            self._users -= 1
            if self._users == 0 and self._client is not None:
                client, self._client = self._client, None
                client.close()
