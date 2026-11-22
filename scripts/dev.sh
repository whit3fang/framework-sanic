#!/usr/bin/env bash
# Local development: API server (SQLite + Redis, fake Dialpad) + UI, seeded with demo data.
# A thin wrapper around `framework-sanic dev` (see `uv run framework-sanic dev --help`):
#
#   scripts/dev.sh                          # server + UI, seeded, prints the URL to open
#   scripts/dev.sh --server-only            # no UI dev server (alias of --ui none)
#   scripts/dev.sh --reset                  # delete the local SQLite database (and flush Redis) first
#   scripts/dev.sh --no-seed                # start empty (you run the connect flows yourself)
#   scripts/dev.sh --connector hubspot      # any installed connector / --features a,b
#   scripts/dev.sh --cache memory           # no Redis
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
if [[ " $* " == *" -h "* || " $* " == *" --help "* ]]; then sed -n '2,10p' "$0"; exec uv run framework-sanic dev --help; fi
uv sync --quiet --all-extras
args=()
for arg in "$@"; do
  case "$arg" in
    --server-only) args+=(--ui none) ;;
    *) args+=("$arg") ;;
  esac
done
exec uv run framework-sanic dev "${args[@]}"
