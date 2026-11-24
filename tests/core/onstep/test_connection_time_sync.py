"""Field bundles b848c925 / df7c66ca (2026-11-24): the driver published no UTC time, so the app's
connect gave up. On that failure the app sends the Raspberry's time and site once through its own
INDI session (as the INDI monitor does), then connects again."""

from __future__ import annotations

import pytest
from astrotool_core.onstep import connection
from astrotool_core.onstep.connection import OnStepConnection
from astrotool_core.testing.fake_onstep_indi_client import fake_indi_runtime_config

_TIME_MISSING = "The driver has not published a UTC time yet (TIME_UTC is empty)"


class _FakeClient:
    """Records calls. The first connect fails with the driver's empty-time error."""

    def __init__(self, *, fail_first_connect_with: str | None) -> None:
        self._fail = fail_first_connect_with
        self.connect_calls = 0
        self.closed = False

    def connect(self, *, timeout: float) -> None:
        self.connect_calls += 1
        if self.connect_calls == 1 and self._fail is not None:
            raise ConnectionError(self._fail)

    def close(self) -> None:
        self.closed = True


class _FakeTransport:
    instances: list[_FakeTransport] = []

    def __init__(self, *, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.writes: list[tuple[str, str, dict[str, object]]] = []
        self.closed = False
        _FakeTransport.instances.append(self)

    def connect(self, *, timeout: float) -> None:
        return None

    def wait_property(self, device: str, name: str, *, timeout: float) -> None:
        return None

    def set_text(self, device: str, name: str, values: dict[str, str], *, timeout: float) -> None:
        self.writes.append((device, name, dict(values)))

    def set_number(
        self, device: str, name: str, values: dict[str, float], *, timeout: float
    ) -> None:
        self.writes.append((device, name, dict(values)))

    def close(self) -> None:
        self.closed = True


def _connection(client: _FakeClient) -> OnStepConnection:
    return OnStepConnection(fake_indi_runtime_config(), client_factory=lambda **_kw: client)


def test_an_empty_driver_time_is_synced_once_and_connect_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced: list[bool] = []
    monkeypatch.setattr(
        connection, "_sync_driver_time", lambda _config, _timeout: synced.append(True)
    )
    client = _FakeClient(fail_first_connect_with=_TIME_MISSING)

    shared = _connection(client).acquire()

    assert shared is client
    assert synced == [True]
    assert client.connect_calls == 2


def test_any_other_connect_failure_is_raised_without_a_time_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced: list[bool] = []
    monkeypatch.setattr(
        connection, "_sync_driver_time", lambda _config, _timeout: synced.append(True)
    )
    client = _FakeClient(fail_first_connect_with="socket closed")

    with pytest.raises(ConnectionError, match="socket closed"):
        _connection(client).acquire()

    assert synced == []
    assert client.closed


def test_the_time_and_site_are_written_through_a_short_indi_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeTransport.instances.clear()
    monkeypatch.setattr(connection, "IndiTransport", _FakeTransport)

    connection._sync_driver_time(fake_indi_runtime_config(), 3.0)

    (session,) = _FakeTransport.instances
    assert session.closed
    names = [name for _device, name, _values in session.writes]
    assert names == ["TIME_UTC", "GEOGRAPHIC_COORD"]
    time_values = session.writes[0][2]
    assert set(time_values) == {"UTC", "OFFSET"}
    assert "T" in str(time_values["UTC"])
