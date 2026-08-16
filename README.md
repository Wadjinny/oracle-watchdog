# oracle-watchdog

Retries launching an Always Free Ampere A1 (`VM.Standard.A1.Flex`) instance on Oracle Cloud Infrastructure until a region has host capacity.

OCI's free A1 capacity in busy regions is often exhausted. This script polls `LaunchInstance` on an interval and creates the instance when a slot opens. It only retries against your own tenancy with your own credentials.

## Setup

1. Put API credentials under `secrets/` (gitignored):

   - `oci_config` — OCI API config (`DEFAULT` profile; `key_file` is relative to `secrets/`)
   - `oracle.pem` — API signing private key
   - `id_ed25519.pub` — SSH public key injected into the instance

2. Edit the `CONFIG` section in `oracle-autocreate.py` with your tenancy values:

   - `COMPARTMENT_ID`, `SUBNET_ID`, `IMAGE_ID`
   - `AVAILABILITY_DOMAINS`, `REGION`
   - shape / memory pairs in `SHAPE_CONFIGS`

## Run locally

```bash
uv sync
uv run python oracle-autocreate.py
```

The process keeps retrying until an instance launches or you stop it with Ctrl+C. `main.py` is a placeholder and is not the entry point.
