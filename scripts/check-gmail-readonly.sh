#!/usr/bin/env bash
# Verify Gmail connectivity using only the read-only command surface.
# Never add secrets to this script or pass tokens on its command line.

set -euo pipefail

account="${1:-${GOG_ACCOUNT:-}}"

if [[ -z "$account" ]]; then
  echo "Usage: $0 <gog-account-or-alias>" >&2
  echo "Alternatively set GOG_ACCOUNT before running this script." >&2
  exit 64
fi

if ! command -v gog >/dev/null 2>&1; then
  echo "gog is not on PATH. Install it in this WSL environment first." >&2
  exit 127
fi

# `--readonly` blocks mutations even if credentials were accidentally over-scoped.
# `--gmail-no-send` blocks Gmail send operations; the exact allowlist permits search only.
exec gog \
  --readonly \
  --gmail-no-send \
  --no-input \
  --enable-commands-exact gmail.search \
  --account "$account" \
  --json \
  --wrap-untrusted \
  gmail search 'in:inbox newer_than:30d' --max 5
