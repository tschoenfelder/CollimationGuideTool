# vendor/

Temporary home for third-party wheels that aren't installable any other way
yet. Every entry should be removed as soon as its real source exists.

## `onstep_adapter-0.4.1-py3-none-any.whl`

OnStepAdapter 0.4.1 (fixes OnStepAdapter#16 park-without-HOME and #17
already-at-target confirmation timeouts; adds `[indi]
tracking_authority_policy`) was handed over as a local build; as of this
commit GitHub's latest release is still v0.4.0. Vendored so CI and fresh
clones can install it via `pyproject.toml`'s
`onstep-adapter @ file:vendor/onstep_adapter-0.4.1-py3-none-any.whl`.

**Remove once a real release exists**: delete the wheel and this README,
repoint `pyproject.toml` at
`https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.4.1/...`
(after checking the release asset is byte-identical to this one).
