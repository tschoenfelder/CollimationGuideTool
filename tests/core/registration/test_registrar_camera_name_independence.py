"""Issue #29 AC (S7): core registration logic is independent of hard-coded Main/Guide
camera names and of the camera count.

Two checks: (1) a static one -- no registrar/geometry module of
`astrotool_core.registration` names a camera ("main"/"guide") in code (identifiers or
non-docstring string literals); (2) a behavioral one -- the registrar's result does not
depend on the `OpticalPrior.name` labels it is given."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from astrotool_core.registration.artificial_star_registrar import ArtificialStarRegistrar
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.testing.frame_factory import single_star_image

_PACKAGE = Path(__file__).resolve().parents[3] / "packages" / "astrotool_core" / "registration"

#: The registration LOGIC modules. `alignment.py` is deliberately not listed: it renders
#: user-facing guidance text that names "Main"/"Guide" (presentation, recorded in
#: proofs/29-cross-camera-registration.toml `gaps`), not registration geometry.
_LOGIC_MODULES = (
    "artificial_star_registrar.py",
    "terrestrial_registrar.py",
    "star_field_registrar.py",
    "geometry.py",
    "optical_prior.py",
    "result.py",
)
_CAMERA_NAME = re.compile(r"\b(main|guide)\b", re.IGNORECASE)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


@pytest.mark.parametrize("module", _LOGIC_MODULES)
def test_no_registration_logic_module_names_a_camera(module: str) -> None:
    tree = ast.parse((_PACKAGE / module).read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and _CAMERA_NAME.search(node.id.replace("_", " ")):
            offenders.append(f"{module}:{node.lineno} identifier {node.id}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _CAMERA_NAME.search(node.value)
        ):
            offenders.append(f"{module}:{node.lineno} literal {node.value[:40]!r}")
    assert offenders == []


def test_the_result_does_not_depend_on_the_camera_labels() -> None:
    frame_a = single_star_image((120, 120), x=70.0, y=50.0, peak=5000.0, sigma=2.0)
    frame_b = single_star_image((400, 400), x=230.0, y=180.0, peak=5000.0, sigma=2.0)

    def prior(name: str, size: int) -> OpticalPrior:
        return OpticalPrior(
            name=name, sensor_width_px=size, sensor_height_px=size, pixel_scale_arcsec=1.0
        )

    named = ArtificialStarRegistrar().register(
        frame_a, frame_b, prior("main", 120), prior("guide", 400)
    )
    renamed = ArtificialStarRegistrar().register(
        frame_a, frame_b, prior("oag", 120), prior("finder #3", 400)
    )

    assert named.ok and renamed.ok
    assert renamed.status is named.status
    assert renamed.polygon_a_in_b == named.polygon_a_in_b
    assert renamed.diagnostics["matched_point"] == named.diagnostics["matched_point"]
