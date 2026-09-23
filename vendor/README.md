# vendor/

Temporary home for third-party wheels that aren't installable any other way
yet. Not a general dependency-vendoring convention for this project --
every entry here should be removed as soon as its real source exists.

## `onstep_adapter-0.4.0-py3-none-any.whl`

OnStepAdapter >= 0.4.0 (INDI-backed transport, AGENTS.md's mandatory
architecture for this deployment) is not yet pushed, tagged or released on
`github.com/tschoenfelder/OnStepAdapter` -- as of this writing that repo's
`main` branch and its latest GitHub release are both still 0.3.5. The
0.4.0 wheel was built locally by the OnStepAdapter maintainer and handed
over directly; it is vendored here purely so CI (and any fresh clone) can
install it via `pyproject.toml`'s
`onstep-adapter @ file:vendor/onstep_adapter-0.4.0-py3-none-any.whl`.

**Remove this once a real release exists**: delete the wheel and this
README, then repoint `pyproject.toml`'s dependency at
`https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.4.0/...`
(the same pattern the 0.3.x line used).
