"""S6.0e: the OnStepAdapter this app is pinned to, and the simulator's 0.5.0 guide-pulse model
checked against the REAL installed `IndiGuideController`.

`FakeOnStepIndiClient.guide_pulse` (and through it the #51 simulator) is a hand-written model of
OnStepAdapter 0.5.0's guide pulse. Every application/adapter test of the astronomical
reacquisition path runs on that model, so it is replayed here against the installed package's
own controller: same mount state, same mid-pulse events, same chunk outcomes -> the same
`IndiGuidePulseResult` field for field, the same issued chunks and the same tracking state
afterwards (an emergency stop is ABORT + TRACK_OFF in both). The real controller gets a fake
clock (its injectable `monotonic`/`sleeper`) and a transport double whose TIMED_GUIDE property
completes at once; its status source is the fake's own snapshot plus the blockers 0.5.0's status
reader would derive (`_guide_snapshot_blockers`), with a fresh status revision on every read and
the `:GU#` `G` flag cleared unless the scenario keeps it set. What this does NOT cover: the real
status reader and meridian classification (configured phases, see the fake's docstring) and the
INDI wire protocol.
"""

from __future__ import annotations

import itertools
import tomllib
from collections.abc import Callable
from dataclasses import astuple, dataclass, fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import onstep_adapter
import pytest
from astrotool_core.mount import AxisDirection, GuidePulseResult, MountAxis
from astrotool_core.onstep import OnStepMountPulseAdapter
from astrotool_core.onstep.mount_pulse_adapter import _GUIDE_DIRECTION
from astrotool_core.testing.fake_onstep_indi_client import (
    GUIDE_IDLE_TIMEOUT_MESSAGE,
    ONSTEP_ADAPTER_050_GUIDE_EXPORTS,
    ONSTEP_ADAPTER_050_ONLY_EXPORTS,
    FakeIndiGuidePulseResult,
    FakeOnStepIndiClient,
    fake_indi_runtime_config,
    make_fake_onstep_indi_connection,
    simulate_onstep_adapter_041_package,
)
from onstep_adapter import IndiMeridianState
from onstep_adapter.indi_guiding import IndiGuideController, IndiGuidePulseResult
from onstep_adapter.indi_status import IndiMountSnapshot

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_PINNED_WHEEL = (
    "https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.5.0/"
    "onstep_adapter-0.5.0-py3-none-any.whl"
)


class TestThePinnedOnStepAdapter:
    def test_pyproject_pins_the_published_0_5_0_wheel(self) -> None:
        deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
        onstep = [d for d in deps if d.startswith("onstep-adapter")]

        assert onstep == [f"onstep-adapter @ {_PINNED_WHEEL}"]

    def test_the_installed_package_is_0_5_0_with_the_guide_pulse_facade(self) -> None:
        """Fails loudly (instead of silently testing 0.4.1) when the venv is not on the pin."""
        assert onstep_adapter.__version__ == "0.5.0", onstep_adapter.__file__
        assert callable(getattr(onstep_adapter.IndiMount, "guide_pulse", None))
        assert callable(getattr(onstep_adapter.IndiMount, "guide", None))
        assert callable(getattr(onstep_adapter.OnStepIndiClient, "guide_pulse", None))
        assert "guiding" in {f.name for f in fields(IndiMountSnapshot)}  # 0.5.0 indi_status.py:41

    def test_the_fakes_guide_bounds_are_the_installed_ones(self) -> None:
        for name, value in ONSTEP_ADAPTER_050_GUIDE_EXPORTS.items():
            assert getattr(onstep_adapter, name) == value, name
        assert set(ONSTEP_ADAPTER_050_ONLY_EXPORTS) <= set(onstep_adapter.__all__)

    def test_the_fakes_result_type_is_the_installed_ones_field_for_field(self) -> None:
        assert [(f.name, str(f.default)) for f in fields(FakeIndiGuidePulseResult)] == [
            (f.name, str(f.default)) for f in fields(IndiGuidePulseResult)
        ]

    def test_the_simulated_0_4_1_package_hides_every_0_5_0_addition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        simulate_onstep_adapter_041_package(monkeypatch)

        assert onstep_adapter.__version__ == "0.4.1"
        assert not hasattr(onstep_adapter.IndiMount, "guide_pulse")
        assert not hasattr(onstep_adapter.IndiMount, "guide")
        for name in ONSTEP_ADAPTER_050_ONLY_EXPORTS:
            assert not hasattr(onstep_adapter, name), name

        monkeypatch.undo()
        assert onstep_adapter.__version__ == "0.5.0"
        assert hasattr(onstep_adapter.IndiMount, "guide_pulse")


# ---------------------------------------------------------------------------
# The fake's guide-pulse model vs the real installed IndiGuideController.
# ---------------------------------------------------------------------------

_Event = Callable[[FakeOnStepIndiClient], None]


@dataclass(frozen=True)
class _ChunkEvent:
    """What happens around one issued chunk, applied identically to the fake and the real run.

    before_idle: once the chunk's TIMED_GUIDE wait ends (may raise = that wait fails).
    stuck: the `G` flag never clears afterwards (the post-chunk wait expires).
    during_stuck: while that expiring wait runs (seen by its later preflights only).
    after_idle: once the post-chunk wait has passed, before the next preflight."""

    before_idle: _Event | None = None
    stuck: bool = False
    during_stuck: _Event | None = None
    after_idle: _Event | None = None


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _client(policy: str, flags: dict[str, object]) -> FakeOnStepIndiClient:
    client = FakeOnStepIndiClient(
        config=fake_indi_runtime_config(tracking_authority_policy=policy)
    )
    client.connect()
    client.parked = False
    client.tracking = True
    for name, value in flags.items():
        setattr(client, name, value)
    return client


def _boom_timeout(_c: FakeOnStepIndiClient) -> None:
    raise TimeoutError("INDI guide pulse TELESCOPE_TIMED_GUIDE_WE did not complete")


def _boom_alert(_c: FakeOnStepIndiClient) -> None:
    raise RuntimeError("INDI rejected guide pulse TELESCOPE_TIMED_GUIDE_WE")


def _tracking_off(c: FakeOnStepIndiClient) -> None:
    c.tracking = False


def _hard_stop(c: FakeOnStepIndiClient) -> None:
    c.meridian_phase = "hard_stop"


def _flip_required(c: FakeOnStepIndiClient) -> None:
    c.meridian_phase = "flip_required"


def _flip_cleared(c: FakeOnStepIndiClient) -> None:
    c.meridian_phase = "pre_meridian_allowed"


_FLAG_SETS: dict[str, dict[str, object]] = {
    "nominal": {},
    "not_tracking": {"tracking": False},
    "parked": {"parked": True},
    "at_home": {"at_home": True},
    "slewing": {"slewing": True},
    "at_limit": {"at_limit": True},
    "no_home_authority": {"home_authority_established": False},
    "no_time_site": {"time_site_authority": False},
    "pier_side_unknown": {"guide_blockers": ("pier_side_unknown",)},
    "reported_error": {"guide_blockers": ("onstep_reported_error",)},
    "flip_required": {"meridian_phase": "flip_required"},
    "hard_stop": {"meridian_phase": "hard_stop"},
    "post_flip": {"meridian_phase": "post_flip"},
    "post_meridian": {"meridian_phase": "post_meridian_allowed"},
    "stop_unconfirmed": {"stop_confirms": False},
}
#: Per issued chunk index ("last" = the final chunk -> the final preflight).
_EVENTS: dict[str, dict[int | str, _ChunkEvent]] = {
    "none": {},
    "timeout_chunk0": {0: _ChunkEvent(before_idle=_boom_timeout)},
    "alert_chunk1": {1: _ChunkEvent(before_idle=_boom_alert)},
    "tracking_off_chunk0": {0: _ChunkEvent(before_idle=_tracking_off)},
    "tracking_off_last": {"last": _ChunkEvent(before_idle=_tracking_off)},
    "hard_stop_chunk0": {0: _ChunkEvent(before_idle=_hard_stop)},
    "flip_required_chunk0": {0: _ChunkEvent(before_idle=_flip_required)},
    # The flip request exists ONLY during chunk 0's post-chunk wait: its warning can only come
    # from that wait's preflight (indi_guiding.py:221-227).
    "flip_only_in_post_chunk_wait0": {
        0: _ChunkEvent(before_idle=_flip_required, after_idle=_flip_cleared)
    },
    "guide_flag_stuck_chunk0": {0: _ChunkEvent(stuck=True)},
    "guide_flag_stuck_chunk1": {1: _ChunkEvent(stuck=True)},
    # A hard stop arising while the expiring wait polls: only its later preflights see it
    # (indi_guiding.py:290-294) -> a refusal instead of the timeout.
    "hard_stop_during_stuck_wait0": {0: _ChunkEvent(stuck=True, during_stuck=_hard_stop)},
}
#: Bounds, chunk-split edges (a remainder below 20 ms is never sent), out-of-range.
_DURATIONS = (19, 20, 499, 500, 501, 519, 520, 521, 1000, 1020, 5000, 5001)


def _chunk_count(duration_ms: int) -> int:
    return max(1, -(-duration_ms // 500)) if 20 <= duration_ms <= 5000 else 1


def _resolve(spec: dict[int | str, _ChunkEvent], duration_ms: int) -> dict[int, _ChunkEvent]:
    return {
        (_chunk_count(duration_ms) - 1 if k == "last" else int(k)): v for k, v in spec.items()
    }


def _outcome(result: object) -> tuple[object, ...]:
    if isinstance(result, BaseException):
        return ("raised", type(result).__name__, str(result))
    assert isinstance(result, (FakeIndiGuidePulseResult, IndiGuidePulseResult))
    return tuple(astuple(result))


def _run_fake(
    policy: str, flags: dict[str, object], duration_ms: int, events: dict[int, _ChunkEvent]
) -> tuple[tuple[object, ...], FakeOnStepIndiClient, list[int]]:
    client = _client(policy, flags)
    issued: list[int] = []
    current = [_ChunkEvent()]
    original_chunk = client._guide_chunk
    original_wait = client._guide_wait_idle

    def chunk(direction: str, chunk_ms: int) -> None:
        current[0] = events.get(len(issued), _ChunkEvent())
        issued.append(chunk_ms)
        original_chunk(direction, chunk_ms)
        if current[0].stuck:
            client.guide_idle_timeouts = [True]
        if current[0].before_idle is not None:
            current[0].before_idle(client)

    def wait_idle(
        command_timeout: float,
    ) -> tuple[IndiMountSnapshot, IndiMeridianState, tuple[str, ...]]:
        result = original_wait(command_timeout)
        if current[0].after_idle is not None:
            current[0].after_idle(client)
        return result

    def elapse(seconds: float) -> None:
        del seconds  # no clock in this replay
        if current[0].during_stuck is not None:
            current[0].during_stuck(client)

    client._guide_chunk = chunk  # type: ignore[method-assign]
    client._guide_wait_idle = wait_idle  # type: ignore[method-assign]
    client._guide_idle_wait_elapse = elapse  # type: ignore[method-assign]
    try:
        result: object = client.guide_pulse("west", duration_ms)
    except (ValueError, ConnectionError) as exc:
        result = exc
    return _outcome(result), client, issued


def _run_real(
    policy: str, flags: dict[str, object], duration_ms: int, events: dict[int, _ChunkEvent]
) -> tuple[tuple[object, ...], FakeOnStepIndiClient, list[int]]:
    client = _client(policy, flags)
    issued: list[int] = []
    revision = [0]
    guiding = [False]
    current = [_ChunkEvent()]
    reads_since_chunk = [0]
    clock = _Clock()

    class Transport:
        def issue_number(
            self, device: str, name: str, element: str, value: float, *, timeout: float
        ) -> int:
            issued.append(int(value))
            return 1

        def wait_property(
            self, device: str, name: str, *, timeout: float, after_revision: int
        ) -> SimpleNamespace:
            current[0] = events.get(len(issued) - 1, _ChunkEvent())
            guiding[0] = current[0].stuck
            reads_since_chunk[0] = 0
            if current[0].before_idle is not None:
                current[0].before_idle(client)
            return SimpleNamespace(revision=after_revision + 1, state="Ok")

    def observe() -> IndiMountSnapshot:
        # The 1st status read after a chunk is the post-chunk wait's first preflight; the 2nd
        # is either that (expiring) wait's next poll or, once it passed, the next preflight.
        reads_since_chunk[0] += 1
        if reads_since_chunk[0] == 2:
            later = current[0].during_stuck if current[0].stuck else current[0].after_idle
            if later is not None:
                later(client)
        revision[0] += 1
        snapshot = client.observe_mount()
        return replace(
            snapshot,
            blockers=tuple(sorted(client._guide_snapshot_blockers(snapshot))),
            status_revision=revision[0],
            guiding=guiding[0],
        )

    controller = IndiGuideController(
        Transport(),
        "LX200 OnStep",
        observe=observe,
        meridian_status=lambda: client._meridian_for(client.observe_mount()),
        emergency_stop=client.emergency_stop,
        authority_policy=policy,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    try:
        result: object = controller.pulse("west", duration_ms)
    except (ValueError, ConnectionError) as exc:
        result = exc
    return _outcome(result), client, issued


@pytest.mark.parametrize("policy", ["strict", "controller_managed"])
@pytest.mark.parametrize("flag_set", list(_FLAG_SETS))
@pytest.mark.parametrize("event", list(_EVENTS))
def test_the_fake_guide_pulse_matches_the_installed_controller(
    policy: str, flag_set: str, event: str
) -> None:
    flags = _FLAG_SETS[flag_set]
    for duration_ms in _DURATIONS:
        events = _resolve(_EVENTS[event], duration_ms)
        fake, fake_client, fake_issued = _run_fake(policy, flags, duration_ms, events)
        real, real_client, real_issued = _run_real(policy, flags, duration_ms, events)

        context = f"{policy}/{flag_set}/{event}/{duration_ms} ms"
        assert fake == real, context
        assert fake_issued == real_issued, context
        assert fake_client.tracking is real_client.tracking, context


def test_the_replay_reaches_every_outcome_class() -> None:
    """Guards the replay itself: completed, refused unsent, refused after a sent chunk with an
    emergency stop, the post-chunk G-flag timeout, and argument errors all occur."""
    seen: set[str] = set()
    for policy, flag_set, event, duration_ms in itertools.product(
        ["strict"], ["nominal", "parked", "hard_stop"], list(_EVENTS), (300, 1000, 19)
    ):
        events = _resolve(_EVENTS[event], duration_ms)
        outcome, client, issued = _run_real(policy, _FLAG_SETS[flag_set], duration_ms, events)
        if outcome[0] == "raised":
            seen.add("argument_error")
        elif outcome[5]:
            seen.add("completed")
        elif not issued:
            seen.add("refused_unsent")
        elif outcome[-1] and GUIDE_IDLE_TIMEOUT_MESSAGE in str(outcome[-1]):
            seen.add("guide_flag_timeout")
        elif not client.tracking:
            seen.add("stopped_after_sent_chunk")

    assert seen == {
        "argument_error", "completed", "refused_unsent", "guide_flag_timeout",
        "stopped_after_sent_chunk",
    }


# ---------------------------------------------------------------------------
# The PRODUCTION adapter through the REAL 0.5.0 facade and client.
# ---------------------------------------------------------------------------


class _E2eRig:
    """`OnStepMountPulseAdapter.guide_pulse` -> the real `onstep_adapter.IndiMount.guide_pulse`
    -> the real `OnStepIndiClient.guide_pulse` -> the real `IndiGuideController`, built with
    the arguments 0.5.0's `connect()` uses (indi_client.py:198-203) plus a fake clock. Only the
    INDI transport is a stub and the mount state is the fake's (status reads, emergency stop,
    the adapter's follow-up `get_status`). `connect()` itself needs an INDI server and is not
    run here."""

    def __init__(
        self, *, stuck: bool = False, tracking_off_after_chunk: int | None = None
    ) -> None:
        connection, made = make_fake_onstep_indi_connection()
        self.adapter = OnStepMountPulseAdapter(connection)
        self.adapter.connect()
        self.mount_state = made[0]
        self.mount_state.parked = False
        self.mount_state.tracking = True
        self.issued: list[tuple[str, str, int]] = []
        self.clock = _Clock()
        state, issued = self.mount_state, self.issued
        revision = [0]

        class Transport:
            is_open = True

            def issue_number(
                self, device: str, name: str, element: str, value: float, *, timeout: float
            ) -> int:
                issued.append((name, element, int(value)))
                return 1

            def wait_property(
                self, device: str, name: str, *, timeout: float, after_revision: int
            ) -> SimpleNamespace:
                if tracking_off_after_chunk is not None and (
                    len(issued) - 1 == tracking_off_after_chunk
                ):
                    state.tracking = False
                return SimpleNamespace(revision=after_revision + 1, state="Ok")

        def observe() -> IndiMountSnapshot:
            revision[0] += 1
            snapshot = state.observe_mount()
            return replace(
                snapshot,
                blockers=tuple(sorted(state._guide_snapshot_blockers(snapshot))),
                status_revision=revision[0],
                guiding=stuck,
            )

        config = fake_indi_runtime_config()
        real_client = onstep_adapter.OnStepIndiClient(config=config, transport=Transport())
        real_client._guide_controller = IndiGuideController(
            Transport(),
            config.device,
            observe=observe,
            meridian_status=lambda: state._meridian_for(state.observe_mount()),
            emergency_stop=state.emergency_stop,
            authority_policy=config.tracking_authority_policy,
            monotonic=self.clock.monotonic,
            sleeper=self.clock.sleep,
        )
        assert isinstance(real_client.mount, onstep_adapter.IndiMount)
        # The adapter's facade: the real 0.5.0 guide_pulse, the fake's status for the rest.
        facade: Any = self.mount_state.mount
        facade.guide_pulse = real_client.mount.guide_pulse

    def west(self, duration_ms: int) -> GuidePulseResult:
        return self.adapter.guide_pulse(MountAxis.AXIS1, AxisDirection.POSITIVE, duration_ms)


class TestTheProductionAdapterOnTheReal050Facade:
    def test_every_production_direction_is_a_real_0_5_0_direction(self) -> None:
        for direction in _GUIDE_DIRECTION.values():
            assert IndiGuideController.normalize_direction(direction) == direction

    def test_a_nominal_pulse_is_chunked_and_keeps_tracking(self) -> None:
        rig = _E2eRig()

        result = rig.west(1200)

        assert result.accepted and result.sent and result.tracking_preserved
        assert not result.tracking_off
        assert (result.chunks_requested, result.chunks_completed) == (3, 3)
        assert rig.issued == [
            ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_W", 500),
            ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_W", 500),
            ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_W", 200),
        ]
        assert rig.clock.t == pytest.approx(1.2)
        assert rig.mount_state.tracking is True

    def test_a_guide_flag_that_never_clears_ends_with_tracking_off(self) -> None:
        rig = _E2eRig(stuck=True)

        result = rig.west(1200)

        assert not result.accepted and result.sent
        assert result.tracking_off is True and rig.mount_state.tracking is False
        assert (result.chunks_requested, result.chunks_completed) == (3, 0)
        assert len(rig.issued) == 1
        assert result.message.startswith(GUIDE_IDLE_TIMEOUT_MESSAGE)
        assert "NOT tracking" in result.message
        assert rig.clock.t == pytest.approx(0.5 + 3.0, abs=0.06)  # chunk + the 3 s wait

    def test_tracking_lost_after_a_chunk_is_refused_with_tracking_off(self) -> None:
        rig = _E2eRig(tracking_off_after_chunk=0)

        result = rig.west(1200)

        assert not result.accepted and result.sent
        assert result.tracking_off is True
        assert result.chunks_completed == 0 and len(rig.issued) == 1
        assert result.message.startswith(
            "guide pulse requires fresh unparked tracking state with no slew, HOME, fault or "
            "limit"
        )
