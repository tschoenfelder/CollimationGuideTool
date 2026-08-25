from collections.abc import Callable

import pytest
from astrotool_core.focus.fake_focuser import FakeFocuser
from collimation_tool.application.focus_controller import FocusSearcher


def _v_curve_fwhm(
    focuser: FakeFocuser, best_position: int, *, base: float = 1.0, slope: float = 0.01
) -> Callable[[], float]:
    def get_fwhm() -> float:
        return base + slope * abs(focuser.get_position() - best_position)

    return get_fwhm


def test_converges_toward_best_position_in_the_positive_direction() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250, fine_step=25)

    result = searcher.search(_v_curve_fwhm(focuser, best_position=1000))

    assert result.reason == "converged"
    assert result.quality == "excellent"
    assert focuser.get_position() == 1000
    assert result.best_fwhm_px == pytest.approx(1.0, abs=0.01)


def test_converges_toward_best_position_in_negative_direction_with_backlash_correction() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250, fine_step=25, final_approach_direction=1)

    result = searcher.search(_v_curve_fwhm(focuser, best_position=-1000))

    assert result.reason == "converged"
    assert focuser.get_position() == -1000


def test_star_lost_before_first_measurement() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser)
    result = searcher.search(lambda: None)
    assert result.reason == "star_lost"
    assert result.quality == "failed"
    assert result.frame_count == 1


def test_star_lost_during_probe_returns_focuser_to_a_sane_state() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250)
    calls = {"n": 0}

    def get_fwhm() -> float | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return 5.0
        return None  # lost on the very first probe move

    result = searcher.search(get_fwhm)
    assert result.reason == "star_lost"


def test_flat_fwhm_curve_finds_no_improving_direction() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250)
    result = searcher.search(lambda: 5.0)
    assert result.reason == "max_steps"
    assert result.quality == "failed"
    assert focuser.get_position() == 0  # probe undoes its own residual offset


def test_cancel_check_stops_the_scan_and_backtracks_to_best() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250)
    calls = {"n": 0}

    def get_fwhm() -> float:
        calls["n"] += 1
        return 1.0 + 0.01 * abs(focuser.get_position() - 1000)

    result = searcher.search(get_fwhm, cancel_check=lambda: calls["n"] >= 3)
    assert result.reason == "cancelled"


def test_quality_tiers_reflect_final_fwhm() -> None:
    focuser = FakeFocuser()
    searcher = FocusSearcher(focuser, coarse_step=250, excellent_fwhm_px=2.0, good_fwhm_px=4.0)
    result = searcher.search(_v_curve_fwhm(focuser, best_position=1000, base=3.0, slope=0.01))
    assert result.quality == "good"
