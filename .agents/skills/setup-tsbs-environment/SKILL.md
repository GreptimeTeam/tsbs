---
name: setup-tsbs-environment
description: Select, install, verify, and report the Go toolchain used by repository TSBS automation. Use for TSBS prerequisite setup, missing or outdated Go, managed Go installation verification, or locating the exact Go binary selected by benchmark and dataset skills.
---

# Setup TSBS Environment

Use `scripts/setup.py` to prepare the Go toolchain required to build TSBS
binaries. Python 3 is assumed to be available.

## Prepare the environment

Run from the repository root:

```bash
python3 .agents/skills/setup-tsbs-environment/scripts/setup.py prepare
```

Reuse a stable system Go 1.21 or newer. If none is available, download the
official Go 1.21.13 archive, verify its published SHA-256, and install it under
`.benchmarks/environment/go`. Benchmark and dataset skills call this operation
automatically immediately before they need to build a TSBS binary.

Pass `--install-root` only to relocate the managed toolchain. Use `--json` or
`--result-file` for structured output.

## Verify without downloading

```bash
python3 .agents/skills/setup-tsbs-environment/scripts/setup.py verify
```

Verify a suitable system Go or the cached managed toolchain. Report its source,
version, platform, binary path, and checksums. Do not alter `PATH`, `GOROOT`,
`GOPATH`, shell profiles, package managers, system packages, services, or the
user's existing Go installation.
