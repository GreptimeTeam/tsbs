---
name: setup-influxdb3
description: Install checksum-verified latest or version-pinned InfluxDB 3 Core or Enterprise native distributions and prepare reusable local benchmark instances. Use for local InfluxDB 3 installation, instance setup, Enterprise trial/home activation, license-file setup, installation verification, or locating a managed binary for TSBS.
---

# Setup InfluxDB 3

Use `scripts/setup.py` for deterministic installation and instance management.
Read `references/setup.md` before choosing an edition, version, platform, or
Enterprise license flow. Use `$benchmark-influxdb3` after preparing an instance.

## Install the official latest or an exact version

Run from the repository root:

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py install \
  --edition core

python3 .agents/skills/setup-influxdb3/scripts/setup.py install \
  --edition enterprise --version 3.11.1
```

Omitting `--version` resolves the edition-specific latest version from the
official InfluxData installer without executing it. Pass an exact semantic
version for deterministic setup; the literal value `latest` is invalid. The
installer verifies the vendor SHA-256 and complete extracted distribution,
checks `influxdb3 --version`, and publishes atomically.

## Prepare an instance

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py prepare \
  --instance-id core-latest --edition core

python3 .agents/skills/setup-influxdb3/scripts/setup.py prepare \
  --instance-id enterprise-311 --edition enterprise --version 3.11.1
```

Instances use a file object store and stable node/cluster identifiers. Existing
instances are immutable with respect to edition, version, and binary checksum.
Omitting `--version` resolves the official latest at command execution time;
it never upgrades an existing instance automatically.

## Activate Enterprise

For trial or home activation, start the command and ask the user to verify the
email while it waits:

```bash
export INFLUXDB3_LICENSE_EMAIL=USER@example.com
python3 .agents/skills/setup-influxdb3/scripts/setup.py activate \
  --instance-id enterprise-311 --license-type trial
```

Prefer `--license-email-stdin` when supplying the email interactively or from a
secret pipe. It takes precedence over the environment variable, reads exactly
one line, and uses a non-echoing prompt on a terminal:

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py activate \
  --instance-id enterprise-311 --license-type home --license-email-stdin
```

Alternatively pass `--license-file /absolute/path/license.jwt`. Override the
email variable name with `--license-email-env`. Activation output is redacted
while it is streamed and scrubbed again during cleanup. Never record or repeat
the email or license contents. Preserve activation logs on failure and
rerun activation safely after verification.

## Inspect and verify

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py list
python3 .agents/skills/setup-influxdb3/scripts/setup.py inspect --instance-id core-311
python3 .agents/skills/setup-influxdb3/scripts/setup.py verify --instance-id core-311
```

Report the instance ID, edition, exact version, binary path and checksum, and
Enterprise license status. Do not add the binary to `PATH` or alter system
packages or services.
