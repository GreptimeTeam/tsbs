---
name: setup-influxdb3
description: Install checksum-verified InfluxDB 3 Core or Enterprise distributions and prepare reusable file- or S3-backed benchmark workspaces. Use for installation, guided S3 configuration, database setup, Enterprise licensing, verification, or locating a managed binary for TSBS.
---

# Setup InfluxDB 3

Use `scripts/setup.py` for deterministic installation and database management.
Read `references/setup.md` before choosing an edition, version, platform, or
Enterprise license flow. Use `$benchmark-influxdb3` after preparing a database workspace.

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

## Prepare a database workspace

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py prepare \
  --database-id core-latest --edition core

python3 .agents/skills/setup-influxdb3/scripts/setup.py prepare \
  --database-id enterprise-311 --edition enterprise --version 3.11.1
```

Database workspaces use a file object store by default and stable node/cluster identifiers.
Existing workspaces are immutable with respect to edition, version, and binary checksum.
Omitting `--version` resolves the official latest at command execution time;
it never upgrades an existing database workspace automatically.

### Prepare S3 storage

Never ask the user to paste S3 credentials into conversation. Ask only for
non-secret bucket, region, endpoint, and HTTP compatibility choices, then give
the user a local interactive command:

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py configure-s3 \
  --output /secure/path/influxdb3-s3-credentials.json \
  --bucket BUCKET --aws-default-region REGION \
  --aws-endpoint ENDPOINT --aws-allow-http
```

The command prompts without echo, writes an owner-only native AWS credentials
JSON without overwriting, and prints a sanitized `prepare` command.
`--aws-allow-http` is required for an HTTP endpoint. Existing native credential
files can be supplied directly:

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py prepare \
  --database-id core-s3 --edition core --object-store s3 \
  --bucket BUCKET --aws-credentials-file /secure/path/aws.json \
  --aws-default-region REGION
```

The file must contain `aws_access_key_id` and `aws_secret_access_key`; optional
fields are `aws_session_token` and `expiry`. Never read, quote, or repeat its
contents in conversation. Bucket/endpoint identity is immutable while the file
contents may rotate in place.

## Activate Enterprise

For trial or home activation, start the command and ask the user to verify the
email while it waits:

```bash
export INFLUXDB3_LICENSE_EMAIL=USER@example.com
python3 .agents/skills/setup-influxdb3/scripts/setup.py activate \
  --database-id enterprise-311 --license-type trial
```

Prefer `--license-email-stdin` when supplying the email interactively or from a
secret pipe. It takes precedence over the environment variable, reads exactly
one line, and uses a non-echoing prompt on a terminal:

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py activate \
  --database-id enterprise-311 --license-type home --license-email-stdin
```

Alternatively pass `--license-file /absolute/path/license.jwt`. Override the
email variable name with `--license-email-env`. Activation output is redacted
while it is streamed and scrubbed again during cleanup. Never record or repeat
the email or license contents. Preserve activation logs on failure and
rerun activation safely after verification.

## Inspect and verify

```bash
python3 .agents/skills/setup-influxdb3/scripts/setup.py list
python3 .agents/skills/setup-influxdb3/scripts/setup.py inspect --database-id core-311
python3 .agents/skills/setup-influxdb3/scripts/setup.py verify --database-id core-311
```

Report the database ID, edition, exact version, binary path and checksum,
storage type/location, and Enterprise license status. Do not add the binary to `PATH` or alter system
packages or services.
