#!/usr/bin/env bash
# Local development: API server (SQLite + Redis) + Vite UI dev server, seeded with demo data.
#
#   scripts/dev.sh                 # server + UI, seeded, prints the URL to open
#   scripts/dev.sh --server-only   # no UI (no Node/pnpm needed)
#   scripts/dev.sh --reset         # delete the local SQLite database first
#   scripts/dev.sh --no-seed       # start empty (you run the connect flows yourself)
#
# Redis (the cache): reuses one already listening on REDIS_PORT, else starts `redis-server`, else
# a `redis:7-alpine` Docker container. Set CACHE_URL to use another one (memory:// opts out).
#
# Overridable env: API_PORT (8000), UI_PORT (5173), REDIS_PORT (6379), DEV_DB (.local/dev.db),
# plus any setting from src/framework_sanic/core/config.py. Ctrl-C stops everything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"
REDIS_PORT="${REDIS_PORT:-6379}"
DEV_DB="${DEV_DB:-$ROOT/.local/dev.db}"
LOG_DIR="$ROOT/.local/logs"
RUN_UI=1
SEED=1
RESET=0

for arg in "$@"; do
  case "$arg" in
    --server-only) RUN_UI=0 ;;
    --no-seed) SEED=0 ;;
    --reset) RESET=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m[dev]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[dev]\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites -------------------------------------------------------------------------
command -v uv >/dev/null || die "uv is required: https://docs.astral.sh/uv/"
PNPM=()
if [[ $RUN_UI == 1 ]]; then
  if command -v pnpm >/dev/null; then PNPM=(pnpm)
  elif command -v corepack >/dev/null; then PNPM=(corepack pnpm)
  elif command -v npx >/dev/null; then PNPM=(npx --yes pnpm@9.15.0)
  else die "Node.js + pnpm are required for the UI (or run with --server-only)"
  fi
fi
for port in "$API_PORT" $([[ $RUN_UI == 1 ]] && echo "$UI_PORT"); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then die "port $port is already in use"; fi
done

# --- lightweight database (SQLite) ---------------------------------------------------------
mkdir -p "$(dirname "$DEV_DB")" "$LOG_DIR"
if [[ $RESET == 1 ]]; then say "resetting $DEV_DB"; rm -f "$DEV_DB"; fi

# Explicit dev configuration (env vars take precedence over .env).
export PLATFORM_ADAPTER="${PLATFORM_ADAPTER:-fake}"
export CONNECTOR="${CONNECTOR:-fake}"
export FEATURES="${FEATURES:-call_logging,sms_logging,fake.dev_harness}"
export STORAGE_ADAPTER="${STORAGE_ADAPTER:-sqlalchemy}"
export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///$DEV_DB}"
export QUEUE_ADAPTER="${QUEUE_ADAPTER:-memory}"
CACHE_URL_GIVEN="${CACHE_URL:-}"
export CACHE_URL="${CACHE_URL:-redis://127.0.0.1:$REDIS_PORT/0}"
export EMBEDDED_WORKERS="${EMBEDDED_WORKERS:-true}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://localhost:$API_PORT}"
export UI_ORIGINS="${UI_ORIGINS:-http://localhost:$UI_PORT,http://127.0.0.1:$UI_PORT}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export APP_BASE_URL="$PUBLIC_BASE_URL"   # for tools/fake_dialpad.py

# --- process management --------------------------------------------------------------------
PIDS=()
REDIS_CONTAINER=""
cleanup() {
  trap - EXIT INT TERM
  say "stopping..."
  for pid in "${PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; done
  if [[ -n $REDIS_CONTAINER ]]; then docker stop "$REDIS_CONTAINER" >/dev/null 2>&1 || true; fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start() {  # start NAME LOGFILE CMD... in its own process group
  local name=$1 log=$2; shift 2
  if command -v setsid >/dev/null; then setsid "$@" >"$log" 2>&1 &
  else "$@" >"$log" 2>&1 &
  fi
  PIDS+=($!)
  say "$name started (pid $!, log: ${log#$ROOT/})"
}

wait_http() {  # wait_http URL NAME LOG
  for _ in $(seq 1 60); do
    if uv run --quiet python -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=1)" "$1" 2>/dev/null; then return 0; fi
    for pid in "${PIDS[@]}"; do kill -0 "$pid" 2>/dev/null || { tail -20 "$3" >&2; die "$2 exited early"; }; done
    sleep 1
  done
  tail -20 "$3" >&2; die "$2 did not become ready at $1"
}

redis_ping() {  # redis_ping URL [flush]: exit 0 if Redis answers (and flush the db if asked)
  uv run --quiet python - "$@" <<'PY' 2>/dev/null
import sys, redis
r = redis.Redis.from_url(sys.argv[1], socket_timeout=1)
r.ping()
if len(sys.argv) > 2:
    r.flushdb()
PY
}

wait_redis() {
  for _ in $(seq 1 30); do redis_ping "$CACHE_URL" && return 0; sleep 0.5; done
  die "Redis did not answer at $CACHE_URL"
}

say "syncing Python dependencies"
uv sync --quiet --all-extras

# --- cache (Redis) -------------------------------------------------------------------------
if [[ -n $CACHE_URL_GIVEN ]]; then
  say "cache: using CACHE_URL=$CACHE_URL"
elif redis_ping "$CACHE_URL"; then
  say "cache: reusing the Redis already on port $REDIS_PORT"
  if [[ $RESET == 1 ]]; then redis_ping "$CACHE_URL" flush && say "cache: flushed (--reset)"; fi
elif command -v redis-server >/dev/null; then
  start "Redis" "$LOG_DIR/redis.log" redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --save "" --appendonly no
  wait_redis
elif command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  say "cache: starting a redis:7-alpine container on port $REDIS_PORT"
  REDIS_CONTAINER="$(docker run -d --rm --name "framework-sanic-dev-redis-$REDIS_PORT" \
    -p "127.0.0.1:$REDIS_PORT:6379" redis:7-alpine --save "" --appendonly no)"
  wait_redis
else
  die "Redis is required for local development. Install it (e.g. \`sudo apt install redis-server\`,
       \`brew install redis\`), enable Docker, point CACHE_URL at a Redis, or opt out with
       CACHE_URL=memory://"
fi

# --- server --------------------------------------------------------------------------------
start "API server" "$LOG_DIR/server.log" uv run framework-sanic serve --host 127.0.0.1 --port "$API_PORT"
wait_http "http://127.0.0.1:$API_PORT/healthz" "API server" "$LOG_DIR/server.log"
say "API ready on $PUBLIC_BASE_URL  (SQLite: ${DEV_DB#$ROOT/}, cache: $CACHE_URL)"

CALL_ID="$(date +%s)"
PHONE="+14155550100"
if [[ $SEED == 1 ]]; then
  say "seeding: admin connect, CRM connect, demo contact, one logged call"
  uv run --quiet python tools/fake_dialpad.py seed --call-id "$CALL_ID" --phone "$PHONE" \
    || die "seeding failed (see .local/logs/server.log)"
fi

# --- UI ------------------------------------------------------------------------------------
if [[ $RUN_UI == 1 ]]; then
  if [[ ! -d ui/node_modules ]]; then say "installing UI dependencies"; (cd ui && "${PNPM[@]}" install); fi
  say "generating API client types"
  uv run --quiet framework-sanic export-schemas --out ui/packages/api-client/src >/dev/null
  export API_PROXY_TARGET="http://127.0.0.1:$API_PORT" UI_PORT
  start "UI dev server" "$LOG_DIR/ui.log" bash -c "cd ui/packages/ui-shell && exec ${PNPM[*]} exec vite"
  wait_http "http://127.0.0.1:$UI_PORT/app/" "UI dev server" "$LOG_DIR/ui.log"
fi

# --- summary -------------------------------------------------------------------------------
TOKEN="$(uv run --quiet python tools/fake_dialpad.py dev-token)"
echo
say "ready"
echo "  API      $PUBLIC_BASE_URL   (healthz, readyz, /dev/fake-connector/records)"
if [[ $RUN_UI == 1 ]]; then
  ENC_PHONE="${PHONE/+/%2B}"
  echo "  UI       http://localhost:$UI_PORT/app/?id_token=$TOKEN&dev_call_id=$CALL_ID&dev_phone=$ENC_PHONE"
  echo "           (the id_token is stripped from the URL after load: reopen this link to start a new session)"
fi
echo "  Replay   uv run python tools/fake_dialpad.py replay-call --shuffle --duplicates 3"
echo "  Logs     .local/logs/"
echo "  Ctrl-C to stop"
wait -n "${PIDS[@]}" 2>/dev/null || true
say "a process exited; see .local/logs/"
