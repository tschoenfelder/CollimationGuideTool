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
"""

from __future__ import annotations

import threading
from collections.abc import Callable

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
