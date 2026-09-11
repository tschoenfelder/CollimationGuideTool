"""Unit tests for the pure logic in
``scripts/translation_estimator_benchmark.py`` (issues #28/#32) -- the
outcome classification (`_classify`/`_expected_wrapped`, which distinguishes
a genuine `wrong_displacement` from an expected `aliased` result) and the
filename-prefix dataset discovery. Loaded via ``importlib`` the same way
``tests/regressions/test_regression_dataset_intake.py`` loads
``scripts/regression_dataset.py`` -- a script, not a package module.

Does not re-run the script against real corpus data (that's
``tests/local_data/test_translation_offset_against_real_captures.py``'s
job, against the actual estimator); these tests only pin the benchmark
script's own bookkeeping logic against a synthetic ``TranslationOffset``,
independent of hardware/real frames.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from astrotool_core.target.translation_offset import TranslationOffset

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "translation_estimator_benchmark.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("translation_estimator_benchmark", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


teb = _load_script()


class TestExpectedWrapped:
    def test_within_range_is_unchanged(self) -> None:
        assert teb._expected_wrapped(100, 1080) == 100.0

    def test_matches_the_real_evidence_this_was_calibrated_against(self) -> None:
        """A real Guide star frame (1920x1080): applied (0, +640) measured
        back as exactly (0, -440) -- see max_unaliased_shift_px's own
        docstring for the full real-hardware evidence."""
        assert teb._expected_wrapped(640, 1080) == -440.0
        assert teb._expected_wrapped(-640, 1080) == 440.0
        assert teb._expected_wrapped(1000, 1920) == -920.0
        assert teb._expected_wrapped(-1000, 1920) == 920.0


class TestClassify:
    _SHAPE = (1080, 1920)

    def test_exact_recovery_is_correct_match(self) -> None:
        offset = TranslationOffset(dx_px=120.0, dy_px=-30.0, score=0.9)
        outcome = teb._classify(offset, 120, -30, self._SHAPE, tolerance_px=1.0)
        assert outcome == "correct_match"

    def test_within_tolerance_is_still_correct_match(self) -> None:
        offset = TranslationOffset(dx_px=120.4, dy_px=-30.0, score=0.9)
        outcome = teb._classify(offset, 120, -30, self._SHAPE, tolerance_px=1.0)
        assert outcome == "correct_match"

    def test_none_is_low_confidence_rejection(self) -> None:
        outcome = teb._classify(None, 120, -30, self._SHAPE, tolerance_px=1.0)
        assert outcome == "low_confidence_rejection"

    def test_the_exact_circular_alias_of_a_too_large_shift_is_aliased_not_wrong(self) -> None:
        # applied (0, 640) on a 1080-tall frame aliases to (0, -440).
        offset = TranslationOffset(dx_px=0.0, dy_px=-440.0, score=0.9)
        outcome = teb._classify(offset, 0, 640, self._SHAPE, tolerance_px=1.0)
        assert outcome == "aliased"

    def test_neither_the_applied_shift_nor_its_alias_is_wrong_displacement(self) -> None:
        """The one outcome that must never occur in a real run -- pins that
        the classifier actually reports it as such rather than silently
        forgiving a genuine defect."""
        offset = TranslationOffset(dx_px=17.0, dy_px=-3.0, score=0.5)
        outcome = teb._classify(offset, 120, -30, self._SHAPE, tolerance_px=1.0)
        assert outcome == "wrong_displacement"


class TestDiscovery:
    def test_stationary_datasets_are_grouped_by_filename_prefix(self, tmp_path: Path) -> None:
        stationary = tmp_path / "stationary"
        stationary.mkdir()
        for name in ("guide_00.fits", "guide_01.fits", "main_00.fits", "not_matching.fits"):
            (stationary / name).touch()

        found = teb.discover_stationary_datasets(tmp_path)

        assert set(found.keys()) == {"guide", "main"}
        assert len(found["guide"]) == 2
        assert len(found["main"]) == 1

    def test_stationary_datasets_are_not_hardcoded_to_main_or_guide(self, tmp_path: Path) -> None:
        stationary = tmp_path / "stationary"
        stationary.mkdir()
        (stationary / "oag_00.fits").touch()
        (stationary / "oag_01.fits").touch()

        found = teb.discover_stationary_datasets(tmp_path)

        assert "oag" in found

    def test_missing_corpus_dir_discovers_nothing(self, tmp_path: Path) -> None:
        assert teb.discover_stationary_datasets(tmp_path / "absent") == {}
        assert teb.discover_known_shift_bases(tmp_path / "absent") == {}
        assert teb.discover_star_bases(tmp_path / "absent") == {}

    def test_known_shift_and_star_bases_are_discovered_by_camera(self, tmp_path: Path) -> None:
        known_shift = tmp_path / "known_shift"
        known_shift.mkdir()
        (known_shift / "guide_base.fits").touch()
        star = tmp_path / "star"
        star.mkdir()
        (star / "guide_star_base.fits").touch()

        assert set(teb.discover_known_shift_bases(tmp_path).keys()) == {"guide"}
        assert set(teb.discover_star_bases(tmp_path).keys()) == {"guide"}


def test_main_skips_cleanly_and_writes_nothing_when_the_corpus_is_absent(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent_28_corpus"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(teb, "CORPUS_DIR", absent)
        mp.setattr(teb, "OUT_DIR", tmp_path / "out")
        exit_code = teb.main()

    assert exit_code == 0
    assert not (tmp_path / "out").exists()
