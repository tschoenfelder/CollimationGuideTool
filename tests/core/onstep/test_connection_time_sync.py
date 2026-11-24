"""Field bundle b848c925 (2026-11-24): the driver published no UTC time, so the app's connect gave
up. On connect the app now sends the Raspberry's time and site once, then connects again."""

from __future__ import annotations

import pytest
from astrotool_core.onstep.connection import OnStepConnection
from astrotool_core.testing.fake_onstep_indi_client import fake_indi_runtime_config


class _FakeClient:
    """Records calls. The first connect fails with the driver's empty-time error."""

    def __init__(self, *, fail_first_connect_with: str | None) -> None:
        self._fail = fail_first_connect_with
        self.connect_calls = 0
        self.time_sync_calls: list[bool] = []
        self.closed = False

    def connect(self, *, timeout: float) -> None:
        self.connect_calls += 1
        if self.connect_calls == 1 and self._fail is not None:
            raise ConnectionError(self._fail)

    def sync_time_location(self, *, user_approved: bool, timeout: float) -> None:
        self.time_sync_calls.append(user_approved)

    def close(self) -> None:
        self.closed = True


def _connection(client: _FakeClient) -> OnStepConnection:
    return OnStepConnection(
        fake_indi_runtime_config(), client_factory=lambda **_kw: client
    )


def test_an_empty_driver_time_is_synced_once_and_connect_is_retried() -> None:
    client = _FakeClient(
        fail_first_connect_with="The driver has not published a UTC time yet (TIME_UTC is empty)"
    )

    shared = _connection(client).acquire()

    assert shared is client
    assert client.time_sync_calls == [True]
    assert client.connect_calls == 2


def test_any_other_connect_failure_is_raised_without_a_time_sync() -> None:
    client = _FakeClient(fail_first_connect_with="socket closed")

    with pytest.raises(ConnectionError, match="socket closed"):
        _connection(client).acquire()

    assert client.time_sync_calls == []
    assert client.closed
