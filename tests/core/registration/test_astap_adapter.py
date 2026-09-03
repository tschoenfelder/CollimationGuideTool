"""Tests for AstapCliSolver — the real subprocess-invoking path is fully
mocked (no real astap_cli binary anywhere on this machine); only the
adapter's own request-building/response-parsing logic is under test.
See the module's own docstring for the assumed CLI/output contract this
pins."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from astrotool_core.registration.astap_adapter import (
    AstapCliSolver,
    AstapSolveHint,
    AstapSolveStatus,
)


def _write_fits(path: Path, *, with_wcs: bool) -> None:
    from astropy.io import fits

    data = np.zeros((10, 10), dtype=np.float32)
    header = fits.Header()
    if with_wcs:
        header["CTYPE1"] = "RA---TAN"
        header["CTYPE2"] = "DEC--TAN"
        header["CRVAL1"] = 10.0
        header["CRVAL2"] = 20.0
        header["CRPIX1"] = 5.0
        header["CRPIX2"] = 5.0
        header["CD1_1"] = -0.001
        header["CD1_2"] = 0.0
        header["CD2_1"] = 0.0
        header["CD2_2"] = 0.001
    fits.PrimaryHDU(data=data, header=header).writeto(path, overwrite=True)


class TestAvailability:
    def test_reports_unavailable_when_binary_is_not_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        solver = AstapCliSolver(binary_path="not-a-real-binary")
        assert not solver.is_available()

    def test_solve_returns_unavailable_without_invoking_a_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        calls = 0

        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            raise AssertionError("subprocess.run must not be called when unavailable")

        monkeypatch.setattr(subprocess, "run", fake_run)
        solver = AstapCliSolver(binary_path="not-a-real-binary")
        result = solver.solve(tmp_path / "frame.fits")

        assert result.status is AstapSolveStatus.ASTAP_UNAVAILABLE
        assert not result.ok
        assert calls == 0


class TestSolve:
    def test_a_successful_solve_returns_a_celestial_wcs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fits_path = tmp_path / "frame.fits"
        _write_fits(fits_path, with_wcs=True)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/astap_cli")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(a, returncode=0, stdout="", stderr=""),
        )
        solver = AstapCliSolver()

        result = solver.solve(fits_path)

        assert result.status is AstapSolveStatus.SOLVED
        assert result.ok
        assert result.wcs is not None
        assert result.wcs.has_celestial

    def test_a_fits_file_with_no_wcs_after_running_is_solve_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fits_path = tmp_path / "frame.fits"
        _write_fits(fits_path, with_wcs=False)  # "astap ran but didn't solve"
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/astap_cli")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a, returncode=1, stdout="", stderr="solution not found"
            ),
        )
        solver = AstapCliSolver()

        result = solver.solve(fits_path)

        assert result.status is AstapSolveStatus.SOLVE_FAILED
        assert not result.ok
        assert "solution not found" in result.message

    def test_a_timeout_is_reported_distinctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fits_path = tmp_path / "frame.fits"
        _write_fits(fits_path, with_wcs=False)
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/astap_cli")

        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="astap_cli", timeout=1.0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        solver = AstapCliSolver(timeout_s=1.0)

        result = solver.solve(fits_path)

        assert result.status is AstapSolveStatus.TIMEOUT
        assert not result.ok

    def test_a_missing_output_file_is_solve_failed_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/astap_cli")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(a, returncode=0, stdout="", stderr=""),
        )
        solver = AstapCliSolver()

        result = solver.solve(tmp_path / "does_not_exist.fits")

        assert result.status is AstapSolveStatus.SOLVE_FAILED
        assert not result.ok


class TestBuildArgs:
    def test_includes_the_fits_path_and_downsample(self, tmp_path: Path) -> None:
        solver = AstapCliSolver(binary_path="astap_cli", downsample=3)
        args = solver._build_args(tmp_path / "x.fits", None)
        assert args[0] == "astap_cli"
        assert str(tmp_path / "x.fits") in args
        assert "-z" in args and args[args.index("-z") + 1] == "3"
        assert "-update" in args

    def test_a_hint_translates_ra_hours_and_south_pole_distance(self, tmp_path: Path) -> None:
        solver = AstapCliSolver()
        hint = AstapSolveHint(ra_deg=150.0, dec_deg=-30.0, radius_deg=5.0, fov_deg=1.5)
        args = solver._build_args(tmp_path / "x.fits", hint)
        assert args[args.index("-ra") + 1] == str(150.0 / 15.0)
        assert args[args.index("-spd") + 1] == str(-30.0 + 90.0)
        assert args[args.index("-r") + 1] == "5.0"
        assert args[args.index("-fov") + 1] == "1.5"

    def test_no_hint_omits_every_hint_flag(self, tmp_path: Path) -> None:
        solver = AstapCliSolver()
        args = solver._build_args(tmp_path / "x.fits", None)
        assert "-ra" not in args
        assert "-spd" not in args
        assert "-r" not in args
        assert "-fov" not in args

    def test_extra_args_are_appended(self, tmp_path: Path) -> None:
        solver = AstapCliSolver(extra_args=("-wcs",))
        args = solver._build_args(tmp_path / "x.fits", None)
        assert args[-1] == "-wcs"
