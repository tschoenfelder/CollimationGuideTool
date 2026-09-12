"""Tests for the autofocus hard safety envelope — issue #33's own most
emphasized requirement: every commanded position must satisfy both the
device's own limits AND a ±1000-step-from-start envelope, whichever is
tighter."""

from __future__ import annotations

from astrotool_core.focus.search_bounds import FocuserSearchBounds, compute_search_bounds


class TestComputeSearchBounds:
    def test_envelope_is_centered_on_the_start_position(self) -> None:
        bounds = compute_search_bounds(5000, 100_000, envelope_steps=1000)
        assert bounds.allowed_min == 4000
        assert bounds.allowed_max == 6000

    def test_device_max_clamps_the_envelope_when_tighter(self) -> None:
        # Starting near the top of a small device range -- the device's own
        # ceiling binds before the ±1000 envelope would.
        bounds = compute_search_bounds(4900, 5000, envelope_steps=1000)
        assert bounds.allowed_max == 5000
        assert bounds.allowed_min == 3900

    def test_device_min_clamps_the_envelope_when_tighter(self) -> None:
        bounds = compute_search_bounds(500, 100_000, device_min_position=0, envelope_steps=1000)
        assert bounds.allowed_min == 0
        assert bounds.allowed_max == 1500

    def test_device_min_defaults_to_zero(self) -> None:
        bounds = compute_search_bounds(200, 100_000, envelope_steps=1000)
        assert bounds.allowed_min == 0

    def test_custom_envelope_size(self) -> None:
        bounds = compute_search_bounds(1000, 100_000, envelope_steps=250)
        assert bounds.allowed_min == 750
        assert bounds.allowed_max == 1250


class TestFocuserSearchBoundsClampAndContains:
    def test_contains_true_inside_range(self) -> None:
        bounds = FocuserSearchBounds(allowed_min=4000, allowed_max=6000)
        assert bounds.contains(4000)
        assert bounds.contains(6000)
        assert bounds.contains(5000)

    def test_contains_false_outside_range(self) -> None:
        bounds = FocuserSearchBounds(allowed_min=4000, allowed_max=6000)
        assert not bounds.contains(3999)
        assert not bounds.contains(6001)

    def test_clamp_leaves_in_range_values_unchanged(self) -> None:
        bounds = FocuserSearchBounds(allowed_min=4000, allowed_max=6000)
        assert bounds.clamp(5000) == 5000

    def test_clamp_pulls_low_values_up_to_the_minimum(self) -> None:
        bounds = FocuserSearchBounds(allowed_min=4000, allowed_max=6000)
        assert bounds.clamp(1000) == 4000

    def test_clamp_pulls_high_values_down_to_the_maximum(self) -> None:
        bounds = FocuserSearchBounds(allowed_min=4000, allowed_max=6000)
        assert bounds.clamp(9000) == 6000
