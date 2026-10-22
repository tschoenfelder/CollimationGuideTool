"""#55 D05 (S6.9): the one owner of the optical-train names and their matching rule."""

from __future__ import annotations

import pytest
from astrotool_core.filter_wheel import config as filter_wheel_config
from astrotool_core.optics.train import GUIDE_TRAIN, MAIN_TRAIN, train_key

pytestmark = pytest.mark.unit


def test_the_names_and_the_case_insensitive_rule_are_unchanged() -> None:
    assert (MAIN_TRAIN, GUIDE_TRAIN) == ("Main", "Guide")
    assert train_key("Main") == train_key("main") == train_key("MAIN") == "main"
    assert train_key(GUIDE_TRAIN) == "guide"
    assert train_key("OAG") != train_key(MAIN_TRAIN)


def test_the_built_in_active_train_is_the_main_train() -> None:
    assert filter_wheel_config._BUILT_IN_ACTIVE_TRAIN == "main" == train_key(MAIN_TRAIN)
