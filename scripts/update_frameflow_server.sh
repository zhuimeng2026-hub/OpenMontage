#!/usr/bin/env bash
# Update and deploy the FrameFlow BFF/MCP services on the production host.
set -Eeuo pipefail

REPO="/opt/OpenMontage"
BRANCH="main"
NO_PULL=0
SKIP_TESTS=0
BFF_BIN="/opt/OpenMontage/frameflow/bff/frameflow-bff"
BFF_SERVICE="frameflow-bff.service"
MCP_SERVICE="openmontage-mcp.service"
TMP_DIR=""
BACKUP_BIN=""
BACKUP_DIR="${BACKUP_DIR:-/var/backups/frameflow}"
ROLLBACK_NEEDED=0

usage() {
  cat <<'EOF'
Usage: update_frameflow_server.sh [options]

Options:
  --repo PATH       OpenMontage repository (default: /opt/OpenMontage)
  --branch NAME     branch to update (default: main)
  --no-pull         do not pull; deploy the current checkout
  --skip-tests      skip Python and Go tests (not recommended)
  --help            show this help
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo requires a path"; REPO="$2"; shift 2 ;;
    --branch) [[ $# -ge 2 ]] || die "--branch requires a name"; BRANCH="$2"; shift 2 ;;
    --no-pull) NO_PULL=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

BFF_BIN="$REPO/frameflow/bff/frameflow-bff"

echo "BFF service: $BFF_SERVICE"
echo "MCP service: $MCP_SERVICE"

redact_journal() {
  # Keep diagnostics useful while avoiding accidental credentials in logs.
  sed -E 's/((token|secret|password|authorization|cookie|api[_-]?key)[=:][^[:space:]]*)/\2=[REDACTED]/Ig'
}

show_journal() {
  echo "--- recent BFF journal (redacted) ---" >&2
  sudo journalctl -u "$BFF_SERVICE" -n 80 --no-pager 2>/dev/null | redact_journal >&2 || true
  echo "--- recent MCP journal (redacted) ---" >&2
  sudo journalctl -u "$MCP_SERVICE" -n 80 --no-pager 2>/dev/null | redact_journal >&2 || true
}

rollback() {
  local status=$?
  if ((status != 0)) && ((ROLLBACK_NEEDED == 1)) && [[ -n "$BACKUP_BIN" ]] && sudo test -f "$BACKUP_BIN"; then
    echo "Deployment failed; restoring previous BFF binary." >&2
    sudo systemctl stop "$BFF_SERVICE" || true
    sudo install -m 0755 "$BACKUP_BIN" "$BFF_BIN" || true
    sudo systemctl start "$BFF_SERVICE" || true
  fi
  if ((status != 0)); then show_journal; fi
  if [[ -n "$TMP_DIR" ]]; then
    rm -f -- "$TMP_DIR/frameflow-bff.new"
    rmdir -- "$TMP_DIR" 2>/dev/null || true
  fi
  exit "$status"
}
trap rollback EXIT

command -v git >/dev/null || die "git is required"
command -v go >/dev/null || die "go is required"
command -v curl >/dev/null || die "curl is required"
[[ -d "$REPO/.git" ]] || die "not a git repository: $REPO"
[[ -f "$REPO/frameflow/bff/go.mod" ]] || die "BFF go.mod not found"

cd "$REPO"
if [[ -n "$(git status --porcelain)" ]]; then
  die "working tree is dirty; commit or stash changes before updating"
fi

current_branch="$(git branch --show-current)"
[[ "$current_branch" == "$BRANCH" ]] || die "checked-out branch is '$current_branch', expected '$BRANCH'"
if ((NO_PULL == 0)); then
  git pull --ff-only origin "$BRANCH"
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x /root/.pyenv/versions/3.11.8/bin/python3 ]]; then
    PYTHON_BIN=/root/.pyenv/versions/3.11.8/bin/python3
  else
    PYTHON_BIN=python3
  fi
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]] || die "Python not found: $PYTHON_BIN"

if ((SKIP_TESTS == 0)); then
  if "$PYTHON_BIN" -c 'import pytest' >/dev/null 2>&1; then
    echo "Running Python upload tests..."
    "$PYTHON_BIN" -m pytest -q tests/test_asset_upload_chunk.py
  else
    echo "WARNING: pytest is not installed; skipping Python upload tests." >&2
  fi
  echo "Running BFF Go tests..."
  (cd "$REPO/frameflow/bff" && go test ./...)
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/frameflow-update.XXXXXX")"
echo "Building BFF binary..."
(cd "$REPO/frameflow/bff" && go build -trimpath -o "$TMP_DIR/frameflow-bff.new" .)
[[ -s "$TMP_DIR/frameflow-bff.new" ]] || die "Go build produced no binary"

timestamp="$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0755 "$BACKUP_DIR"
BACKUP_BIN="${BACKUP_DIR}/frameflow-bff.${timestamp}"
if sudo test -f "$BFF_BIN"; then
  sudo cp -p "$BFF_BIN" "$BACKUP_BIN"
else
  die "existing BFF binary not found: $BFF_BIN"
fi

sudo systemctl stop "$BFF_SERVICE"
ROLLBACK_NEEDED=1
sudo install -m 0755 "$TMP_DIR/frameflow-bff.new" "$BFF_BIN"

# BFF and MCP are independent deployments. Restart both directly; do not
# inspect, disable, or infer dependencies from any legacy unit.
sudo systemctl restart "$BFF_SERVICE"
sudo systemctl restart "$MCP_SERVICE"

echo "Checking service health..."
[[ "$(sudo systemctl is-active "$BFF_SERVICE")" == active ]] || die "$BFF_SERVICE is not active"
[[ "$(sudo systemctl is-active "$MCP_SERVICE")" == active ]] || die "$MCP_SERVICE is not active"

wait_http() {
  local url="$1" expected="$2" label="$3" code="" attempt
  for attempt in $(seq 1 30); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || true)"
    [[ "$code" == "$expected" ]] && return 0
    sleep 1
  done
  die "$label returned HTTP ${code:-no response} (expected $expected)"
}
wait_http http://127.0.0.1:8080/api/me 200 "BFF /api/me"
wait_http http://127.0.0.1:8900/mcp 401 "MCP /mcp"

check_pid() {
  local service="$1" port="$2" main_pid listener_pids
  main_pid="$(sudo systemctl show "$service" -p MainPID --value)"
  [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || die "$service has no valid MainPID"
  listener_pids="$(sudo ss -lntp "( sport = :$port )" 2>/dev/null | grep -oE 'pid=[0-9]+' | sed 's/pid=//' | sort -u | tr '\n' ' ' || true)"
  [[ -n "$listener_pids" ]] || die "$service has no listener on :$port"
  [[ " $listener_pids " == *" $main_pid "* ]] || die "$service MainPID $main_pid is not listening on :$port (PIDs: $listener_pids)"
}
check_pid "$BFF_SERVICE" 8080
check_pid "$MCP_SERVICE" 8900

ROLLBACK_NEEDED=0
head="$(git rev-parse --short HEAD)"
echo "Build metadata:"
go version -m "$TMP_DIR/frameflow-bff.new" | grep -E 'vcs\.revision|vcs\.modified' || true
echo "Update completed successfully at commit $head. Backup: $BACKUP_BIN"
