# Porting notes: smart_telescope -> CollimationGuideTool

Source-path -> target-path mapping, filled in during each stage's inventory
pass (see PLAN.md Stage 0 and per-stage notes). Not filled in yet — this
file is scaffolding for that record, populated as each stage's port work
happens rather than all at once up front.

## Format

```
### <target path>
- Source: <smart_telescope path> (or: new, no analog)
- Change: <rename / strip coupling / generalize naming / port as-is / ...>
- Stage: <N>
```
