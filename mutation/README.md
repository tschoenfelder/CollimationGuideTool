# Mutation-testing sessions (issue #5)

One [cosmic-ray](https://cosmic-ray.readthedocs.io/) config per module,
since `module-path` takes a single file/package and the four candidate
modules are scattered across the tree. Session databases (`*.sqlite`) are
gitignored — working state, regenerate them locally. The durable output is
`docs/quality/mutation.json` / `mutation.md`, produced by
`scripts/mutation_report.py`.

## Rerunning the baseline

```
python -m pip install -e ".[dev]"    # installs cosmic-ray into .venv
```

Windows note: cosmic-ray's `test-command` is launched directly (not through
a shell), so a bare `pytest` only resolves to this project's venv if
`.venv/Scripts` is on `PATH` — activate the venv, or prepend it for the one
command:

```powershell
$env:PATH = ".venv\Scripts;$env:PATH"
```

```bash
export PATH=".venv/Scripts:$PATH"   # git-bash equivalent
```

Then, per module:

```
cosmic-ray init mutation/<module>.toml mutation/<module>.sqlite
cosmic-ray exec mutation/<module>.toml mutation/<module>.sqlite
cr-rate mutation/<module>.sqlite          # quick score check
```

`<module>` is one of `correction_model`, `collimation_measurement`,
`collimation_state`, `roi_tracker`. `collimation_measurement` has ~2100
mutants and takes 30+ minutes; the other three are a few minutes each.

After all four sessions have run:

```
python scripts/mutation_report.py
```

writes the combined report. `git diff docs/quality/mutation.md` shows
whether the score moved.

## Scope

Selective per the issue: pure/near-pure deterministic domain modules only.
Explicitly out of scope for this initial baseline: UI, `StreamController`/
threading code, and the touptek/INDI hardware adapters — cosmic-ray mutates
and re-runs the test suite per mutant, which isn't reliable against
threaded or hardware-adjacent code without more setup than a first
iteration warrants.
