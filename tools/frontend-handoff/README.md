# Frontend handoff generator

Run from the repository root with Python 3.11+ and Node 22.12+ (22.x):

```bash
python3 scripts/package_frontend_handoff.py
```

The command snapshots the working tree, applies this directory's overlay only in a temporary copy, creates an unverified candidate, installs dependencies in a fresh extraction, validates it, and publishes a ZIP plus SHA-256 under release/frontend-handoff only when all checks pass. It never modifies production frontend files.

`--prepare-only` produces an explicitly unverified candidate under .runtime/frontend-handoff. `--resume <run-directory>` reuses that frozen source snapshot, reapplies the current overlay and repeats complete validation. `--node-bin <directory>` selects the Node22 installation. Logs, source checksums, full adaptations and browser evidence remain private in the run directory. Resume never treats previous passing logs as new validation.

Overlay owns only handoff behavior. Keep fixtures aligned with current public frontend types. Runtime/data/font asset provenance is described in README.runtime.md. Repository source scripts are copied from an explicit allowlist; adding one requires checking all imports and external dependencies. Snapshot instability, a missing transformation anchor, symlinks, secret scan findings or validation failures prevent publication.
