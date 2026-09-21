"""OnStepConnection — the one owner of this app's single `OnStepClient`.

OnStepAdapter's `OnStepClient` owns the physical serial port exclusively, so
the focuser, park and pulse shims must all share ONE client rather than each
opening their own. Each shim `acquire()`s on `connect()` and `release()`s on
`disconnect()`; the port is opened by the first and closed by the last.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from onstep_adapter import OnStepClient, OnStepSafetyConfig


class OnStepConnection:
    def __init__(
        self,
        port: str,
        *,
        baud_rate: int = 9600,
        timeout: float = 2.0,
        client_factory: Callable[..., OnStepClient] = OnStepClient,
        safety_config: OnStepSafetyConfig | None = None,
    ) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._client_factory = client_factory
        self._safety_config = safety_config
        self._client: OnStepClient | None = None
        self._users = 0
        self._lock = threading.Lock()

    @property
    def port(self) -> str:
        return self._port

    @property
    def client(self) -> OnStepClient | None:
        """The connected client, or None while nobody holds the connection."""
        return self._client

    def acquire(self) -> OnStepClient:
        """Open (first user) or share (later users) the client. Raises
        `ConnectionError` if the controller cannot be reached; a failed
        open leaves no half-open state and no user count behind."""
        with self._lock:
            if self._client is None:
                extra = (
                    {} if self._safety_config is None else {"safety_config": self._safety_config}
                )
                client = self._client_factory(
                    self._port, baud_rate=self._baud_rate, timeout=self._timeout, **extra
                )
                result = client.connect()
                if not result.connected:
                    client.close()
                    raise ConnectionError(f"could not connect to OnStep on {self._port}")
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
