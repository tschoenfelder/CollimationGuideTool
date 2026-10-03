"""Sleep guard (issue #53, CONTRIBUTING.md "Deterministic time").

Production code must not block on the wall clock directly: policy waits
(settle times, retries, confirmation timeouts, deadlines) go through an
injected `astrotool_core.timing.Clock`. This test AST-scans every production
module under `apps/` and `packages/astrotool_core/` for a direct call to the
standard library's sleep (`time.sleep`, `from time import sleep`, aliased
forms) and fails on any file not in `ALLOWLIST`.

Every allowlisted file states why. An entry that no longer sleeps fails too,
so the list only ever shrinks. To add an exception you need a real reason a
reviewer will accept (an external API that requires blocking), not "it was
simpler".

S3b extends the guard to the POLICY layers (`POLICY_ROOTS`: application, UI,
domain, acquisition, mount, focus and filter-wheel code), where two more
wall-clock patterns are policy waits in disguise:

- deadline arithmetic on the real clock -- a `time.monotonic()` /
  `perf_counter()` / `time.time()` (and `_ns`) read whose value is an operand
  of `+`, `-` or a comparison, either directly (`time.monotonic() + t`) or via
  a plain local name in the same function (`now = time.monotonic(); if now -
  t0 > x`) or a `self.<attr>` anywhere in the file. A stamp that never takes
  part in arithmetic (`f(captured_at=time.monotonic())`) is not flagged:
  recording when something happened is not a wait;
- a timed blocking wait -- `<x>.wait(<timeout>)` / `.wait(timeout=...)`
  (Event, Condition, QThread) or `.wait_for(pred, <timeout>)` -- whether or
  not its result is used.

They have their own allowlist, `POLICY_WALL_CLOCK_ALLOWLIST`, shrink-only too.
Both allowlists are pinned by an exact-set test, so ADDING an entry is a
visible test change in review.

Known gaps (not detected): a stamp passed on through a keyword argument,
dataclass field, return value or another object's attribute and compared
elsewhere (e.g. `taken_at=time.monotonic()` checked later via `ref.taken_at`);
names re-bound through tuple unpacking or augmented assignment; `datetime.now()`;
other blocking timeouts (`Thread.join(t)`, `queue.get(timeout=)`, socket/select
timeouts, `threading.Timer`, `QTimer.singleShot`); timed waits in adapter
packages outside `POLICY_ROOTS` (onstep, indi, camera -- hardware boundaries).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Scanned production trees (all of them; the policy layers named in
#: CONTRIBUTING.md and the adapters, whose exceptions are listed below).
SCANNED_ROOTS = ("apps", "packages/astrotool_core")

#: Test doubles and the clock itself are not production policy code.
EXCLUDED_PREFIXES = ("packages/astrotool_core/testing/",)

ALLOWLIST: dict[str, str] = {
    "packages/astrotool_core/timing/clock.py": (
        "SystemClock is the one sanctioned production sleep: everything else waits through it"
    ),
    "packages/astrotool_core/camera/touptek_adapter.py": (
        "hardware adapter: _prepare_capture_mode lets the SDK settle 0.2 s in video mode "
        "after StartPullModeWithCallback before switching to software trigger (without it: "
        "stale/black frames, smart_telescope M10 hardware history); vendor-API blocking"
    ),
}

#: Policy layers (S3b) scanned for wall-clock deadline arithmetic and waits used as sleeps.
POLICY_ROOTS = (
    "apps/*/application",
    "apps/*/ui",
    "packages/astrotool_core/acquisition",
    "packages/astrotool_core/mount",
    "packages/astrotool_core/focus",
    "packages/astrotool_core/filter_wheel",
    "apps/*/domain",
)

POLICY_WALL_CLOCK_ALLOWLIST: dict[str, str] = {
    "apps/collimation_tool/ui/mount_test_move_panel.py": (
        "pending S6.6/S6.7: the capture-job timeout, the reusable reference's max age and the "
        "motion-completed reference are measured on the real clock in the panel itself; the "
        "clock arrives with the Mount Align orchestration leaving the panel (the runner it "
        "drives is on the injected clock since S3b)"
    ),
    "packages/astrotool_core/acquisition/stream_controller.py": (
        "FrameMailbox.wait_latest/wait_next_after block on a Condition until a real-time "
        "deadline, and the capture loop's cadence wait is a stop-event wait used as a sleep; "
        "frames are stamped on the same real clock the camera delivers in. A clock seam here "
        "is the optional S4/#51 injection, not yet taken (recorded as an S3b dependency)"
    ),
    "packages/astrotool_core/filter_wheel/indi_filter_wheel_adapter.py": (
        "_maybe_refresh_properties throttles getProperties re-requests to one per 2 s on the "
        "real clock (S4 finding (c): the adapter has no clock/client injection yet) -> S6.3; "
        "a rate limit on INDI traffic, not a confirmation timeout"
    ),
}


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if "__pycache__" in rel or rel.startswith(EXCLUDED_PREFIXES):
                continue
            files.append(path)
    return files


def direct_sleep_calls(source: str) -> list[int]:
    """Line numbers of direct stdlib sleep calls in `source`: `time.sleep` calls
    (also via `import time as t`) and `sleep(...)` imported with
    `from time import sleep [as x]`. Passing a sleep function around as a
    value (e.g. a default argument) also counts: it is still a wall-clock wait."""
    tree = ast.parse(source)
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    module_aliases.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "sleep":
                    function_aliases.add(alias.asname or "sleep")
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "sleep"
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ) or (
            isinstance(node, ast.Name)
            and node.id in function_aliases
            and isinstance(node.ctx, ast.Load)
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


#: Real-clock reads: monotonic/perf_counter and calendar time (a `time.time()` deadline is
#: still a real-time deadline).
_WALL_CLOCK_READS = frozenset(
    {"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "time", "time_ns"}
)
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _time_aliases(tree: ast.AST, names: frozenset[str]) -> tuple[set[str], set[str]]:
    """(aliases of the `time` module, local names bound by `from time import <names>`)."""
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    module_aliases.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name in names:
                    function_aliases.add(alias.asname or alias.name)
    return module_aliases, function_aliases


def _is_arithmetic_operand(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    return isinstance(parent, ast.Compare) or (
        isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Sub))
    )


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The nearest enclosing function (None = module level)."""
    current = parents.get(node)
    while current is not None and not isinstance(current, _FUNCTION_SCOPES):
        current = parents.get(current)
    return current


def _is_wall_clock_read(
    node: ast.Call, module_aliases: set[str], function_aliases: set[str]
) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _WALL_CLOCK_READS
        and isinstance(func.value, ast.Name)
        and func.value.id in module_aliases
    ) or (isinstance(func, ast.Name) and func.id in function_aliases)


def _assignment_targets(parent: ast.AST | None) -> list[ast.expr]:
    if isinstance(parent, ast.Assign):
        return list(parent.targets)
    if isinstance(parent, (ast.AnnAssign, ast.NamedExpr)):
        return [parent.target]
    return []


def wall_clock_deadline_reads(source: str) -> list[int]:
    """Line numbers of real-clock reads (`time.monotonic()`, `perf_counter()`, `time.time()`,
    their `_ns` forms; aliased or via `from time import`) whose value is used in deadline
    arithmetic: the read is itself an operand of `+`, `-` or a comparison, OR it is assigned
    to a plain local name (tracked within the same function) or to `self.<attr>` (tracked
    file-wide) that is later such an operand -- `now = time.monotonic(); if now - t0 > x`.
    A stamp that never takes part in arithmetic (`f(captured_at=time.monotonic())`) is not
    flagged."""
    tree = ast.parse(source)
    module_aliases, function_aliases = _time_aliases(tree, _WALL_CLOCK_READS)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    lines: set[int] = set()
    local_stamps: dict[tuple[ast.AST | None, str], list[int]] = {}
    attr_stamps: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_wall_clock_read(node, module_aliases, function_aliases):
            continue
        parent = parents.get(node)
        if _is_arithmetic_operand(node, parents) or (
            isinstance(parent, ast.NamedExpr) and _is_arithmetic_operand(parent, parents)
        ):
            lines.add(node.lineno)
            continue
        for target in _assignment_targets(parent):
            if isinstance(target, ast.Name):
                key = (_scope(node, parents), target.id)
                local_stamps.setdefault(key, []).append(node.lineno)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                attr_stamps.setdefault(target.attr, []).append(node.lineno)
    for node in ast.walk(tree):
        if not _is_arithmetic_operand(node, parents):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            lines.update(local_stamps.get((_scope(node, parents), node.id), ()))
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            lines.update(attr_stamps.get(node.attr, ()))
    return sorted(lines)


#: Receivers whose `.wait(...)` is not a timed thread wait (asyncio/futures take task sets).
_NON_TIMER_WAIT_RECEIVERS = frozenset({"asyncio", "futures", "concurrent"})
#: Timed wait methods -> how many positional args make the timeout present.
_TIMED_WAIT_POSITIONAL_TIMEOUT = {"wait": 1, "wait_for": 2}


def timed_waits(source: str) -> list[int]:
    """Line numbers of real-time blocking waits with a timeout: `<x>.wait(<timeout>)` /
    `<x>.wait(timeout=...)` (Event, Condition, QThread...) and `<x>.wait_for(pred, <timeout>)`
    -- whether or not the result is used (`while not stop.wait(0.5):` still waits on the real
    clock). A wait without a timeout (a join / an unbounded condition wait) is not flagged."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _NON_TIMER_WAIT_RECEIVERS:
            continue
        min_positional = _TIMED_WAIT_POSITIONAL_TIMEOUT.get(node.func.attr)
        if min_positional is None:
            continue
        if len(node.args) >= min_positional or any(k.arg == "timeout" for k in node.keywords):
            lines.append(node.lineno)
    return sorted(set(lines))


def _policy_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in POLICY_ROOTS:
        for root in _REPO_ROOT.glob(pattern):
            for path in root.rglob("*.py"):
                if "__pycache__" not in path.parts:
                    files.add(path)
    return sorted(files)


def _policy_offenders() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _policy_files():
        source = path.read_text(encoding="utf-8")
        lines = sorted(set(wall_clock_deadline_reads(source)) | set(timed_waits(source)))
        if lines:
            found[path.relative_to(_REPO_ROOT).as_posix()] = lines
    return found


def _offenders() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _production_files():
        lines = direct_sleep_calls(path.read_text(encoding="utf-8"))
        if lines:
            found[path.relative_to(_REPO_ROOT).as_posix()] = lines
    return found


def test_no_direct_sleep_outside_the_allowlist() -> None:
    unexpected = {path: lines for path, lines in _offenders().items() if path not in ALLOWLIST}
    assert not unexpected, (
        "Direct wall-clock sleep in production code (issue #53). Wait through an injected "
        "astrotool_core.timing.Clock instead, or add a justified ALLOWLIST entry: "
        f"{unexpected}"
    )


def test_every_allowlist_entry_still_sleeps() -> None:
    offenders = _offenders()
    stale = sorted(path for path in ALLOWLIST if path not in offenders)
    assert not stale, f"Remove these ALLOWLIST entries, they no longer sleep: {stale}"


def test_every_allowlist_entry_gives_a_reason() -> None:
    assert all(reason.strip() for reason in ALLOWLIST.values())
    assert all(reason.strip() for reason in POLICY_WALL_CLOCK_ALLOWLIST.values())


def test_policy_roots_exist() -> None:
    for pattern in POLICY_ROOTS:
        assert list(_REPO_ROOT.glob(pattern)), f"POLICY_ROOTS pattern matches nothing: {pattern}"


def test_no_wall_clock_deadline_or_wait_as_sleep_in_policy_code() -> None:
    unexpected = {
        path: lines
        for path, lines in _policy_offenders().items()
        if path not in POLICY_WALL_CLOCK_ALLOWLIST
    }
    assert not unexpected, (
        "Real-clock deadline arithmetic or an Event/Condition wait used as a sleep in policy "
        "code (issue #53). Use an injected astrotool_core.timing.Clock (Deadline, poll_until, "
        "clock.sleep(s, cancel)) or add a justified POLICY_WALL_CLOCK_ALLOWLIST entry: "
        f"{unexpected}"
    )


def test_every_policy_allowlist_entry_still_offends() -> None:
    offenders = _policy_offenders()
    stale = sorted(path for path in POLICY_WALL_CLOCK_ALLOWLIST if path not in offenders)
    assert not stale, f"Remove these POLICY_WALL_CLOCK_ALLOWLIST entries: {stale}"


@pytest.mark.parametrize(
    "path",
    [
        "apps/collimation_tool/ui/mount_test_move_runner.py",
        "apps/collimation_tool/application/recenter_policy.py",
        "apps/collimation_tool/ui/focuser_panel.py",
        "apps/collimation_tool/ui/filter_wheel_panel.py",
        "apps/collimation_tool/ui/mount_park_panel.py",
    ],
)
def test_s3b_modules_wait_only_through_their_clock(path: str) -> None:
    """S3b proof: off both allowlists, and no offence of either kind left."""
    assert path not in ALLOWLIST and path not in POLICY_WALL_CLOCK_ALLOWLIST
    assert path not in _offenders() and path not in _policy_offenders()


_SLEEP = "sleep"  # kept out of literal call syntax so the tier scanner ignores this file


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"import time\ntime.{_SLEEP}(1)\n", [2]),
        (f"import time as t\nt.{_SLEEP}(1)\n", [2]),
        (f"from time import {_SLEEP}\n{_SLEEP}(1)\n", [2]),
        (f"from time import {_SLEEP} as nap\nnap(1)\n", [2]),
        (f"import time\ndef f(s=time.{_SLEEP}):\n    s(1)\n", [2]),
        ("import time\ntime.monotonic()\n", []),
        (f"def f(clock):\n    clock.{_SLEEP}(1)\n", []),  # injected clock: fine
        (f"import asyncio\nasyncio.{_SLEEP}(1)\n", []),
    ],
)
def test_detector(source: str, expected: list[int]) -> None:
    assert direct_sleep_calls(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # a read that is itself an operand of +, - or a comparison
        ("import time\ndeadline = time.monotonic() + 5\n", [2]),
        ("import time\nif time.monotonic() >= deadline:\n    pass\n", [2]),
        ("import time\nok = time.monotonic() - issued > 10\n", [2]),
        ("import time\nremaining = deadline - time.monotonic()\n", [2]),
        ("import time as t\nend = t.perf_counter() + 1\n", [2]),
        ("from time import monotonic\nend = monotonic() + 1\n", [2]),
        ("from time import monotonic as now\nx = now() < end\n", [2]),
        ("import time\nx = time.time() + 5\n", [2]),  # calendar-time deadline: still real time
        ("from time import time_ns\nx = time_ns() > end\n", [2]),
        # stamp-then-compare through a local name (same function)
        (
            "import time\ndef f(self):\n    now = time.monotonic()\n    return now - self.t0 > 9\n",
            [3],
        ),
        ("import time\ndef f():\n    t: float = time.time()\n    return t >= end\n", [3]),
        ("import time\ndef f():\n    if (n := time.monotonic()) > end:\n        pass\n", [3]),
        # ... and through self.<attr> (file-wide)
        (
            "import time\nclass P:\n    def a(self):\n        self._t0 = time.monotonic()\n"
            "    def b(self):\n        return self._t1 - self._t0 > 9\n",
            [4],
        ),
        # not flagged: stamps that never take part in arithmetic
        ("import time\nstamp = time.monotonic()\n", []),
        ("import time\nf(captured_at=time.monotonic())\n", []),
        ("import time\nrecord = {'at': time.time()}\n", []),
        # the same local name in ANOTHER function is a different variable
        (
            "import time\ndef f():\n    now = time.monotonic()\ndef g(now):\n    return now + 1\n",
            [],
        ),
        ("def f(clock):\n    return clock.monotonic() + 5\n", []),  # injected clock: fine
    ],
)
def test_deadline_detector(source: str, expected: list[int]) -> None:
    assert wall_clock_deadline_reads(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("stop.wait(0.5)\n", [1]),
        ("self._stop_event.wait(timeout=s)\n", [1]),
        ("cond.wait(timeout=remaining)\n", [1]),
        ("if stop.wait(0.5):\n    pass\n", [1]),  # result used: still a real-time wait
        ("while not stop.wait(0.5):\n    poll()\n", [1]),
        ("_ = ev.wait(t)\n", [1]),
        ("cond.wait_for(lambda: done, 2.0)\n", [1]),
        ("cond.wait_for(pred, timeout=t)\n", [1]),
        ("worker.wait()\n", []),  # no timeout: a join / unbounded wait, not a timer
        ("cond.wait_for(pred)\n", []),
        ("asyncio.wait(tasks)\n", []),
        ("futures.wait(fs, timeout=1)\n", []),
        (f"clock.{_SLEEP}(1, stop)\n", []),
    ],
)
def test_timed_wait_detector(source: str, expected: list[int]) -> None:
    assert timed_waits(source) == expected


def test_the_allowlists_are_pinned() -> None:
    """Adding an exception must show up in review as a change to THIS test, not only as one
    more dict entry above (the lists are shrink-only by the stale-entry tests)."""
    assert set(ALLOWLIST) == {
        "packages/astrotool_core/timing/clock.py",
        "packages/astrotool_core/camera/touptek_adapter.py",
    }
    assert set(POLICY_WALL_CLOCK_ALLOWLIST) == {
        "apps/collimation_tool/ui/mount_test_move_panel.py",
        "packages/astrotool_core/acquisition/stream_controller.py",
        "packages/astrotool_core/filter_wheel/indi_filter_wheel_adapter.py",
    }
