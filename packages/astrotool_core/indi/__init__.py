"""A small, generic INDI protocol client -- see `client.py`'s docstring.

Its only production user is the filter wheel (`astrotool_core.filter_wheel`). It must never
reach the OnStep device: mount and focuser go through OnStepAdapter only (AGENTS.md), enforced
by an import-linter contract (only `filter_wheel` and `testing` may import
`astrotool_core.indi.client`). S6.8d review: nothing is re-exported here, so the contract
cannot be bypassed through the package.
"""
