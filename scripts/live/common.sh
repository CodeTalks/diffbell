# shellcheck shell=bash
# shellcheck disable=SC2034 # the variables are used by the scripts that source this
# Shared settings for the live-check scripts (bot onboarding spec §4, "Live checks").
# Sourced by the other scripts in this folder, never run on its own. Every path,
# Keychain service and launchd label here is scratch: nothing points at the live
# config, state, logs, Keychain items, launchd agent or Watchman.

# Everything these scripts create (watchman.env, the scratch folder, the probe's
# temporary plist) is ours alone: 0600 files, 0700 folders (ADR 0007). `dbell` restores
# the caller's umask before it starts diffbell, so the live check sees the modes
# diffbell sets itself.
ORIG_UMASK="$(umask)"
umask 077

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

LIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$LIVE_DIR/../.." && pwd)"
PY="$REPO/.venv/bin/python"
DBELL_BIN="$REPO/.venv/bin/diffbell"

DEFAULT_SERVICE="io.688.diffbell" # the live service: only ever compared against
SCRATCH_SERVICE="io.688.diffbell-scratch"
SCRATCH_LABEL="io.688.diffbell-scratch"
SCRATCH_ROOT="$HOME/.cache/diffbell-scratch"
SCRATCH_PORT=8799
ACCOUNTS=(bot-token chat-id save-pending probe)

CONFIG="$SCRATCH_ROOT/config/diffbell/config.yaml"
STATE="$SCRATCH_ROOT/state"
LOGS="$SCRATCH_ROOT/logs"
PROJECT="$SCRATCH_ROOT/project"
WM_BASE="$SCRATCH_ROOT/wm"
WM_INFO="$SCRATCH_ROOT/watchman.env"

# The scratch service and label must differ from the live ones and keep the prefix
# secrets.service() enforces.
[[ "$SCRATCH_SERVICE" != "$DEFAULT_SERVICE" && "$SCRATCH_SERVICE" == "$DEFAULT_SERVICE"-* ]] ||
  die "the scratch Keychain service is misconfigured"
[[ "$SCRATCH_LABEL" != "$DEFAULT_SERVICE" ]] || die "the scratch launchd label is the live one"
[[ "$SCRATCH_ROOT" == "$HOME/.cache/diffbell-scratch" ]] || die "unexpected scratch root"

require_venv() {
  [[ -x "$PY" && -x "$DBELL_BIN" ]] || die "no $REPO/.venv: run \`uv sync\` in the worktree first"
}

# Succeeds only when the scratch item is known to be absent (security's exit 44,
# "not found"). Any other failure of `security` is an error, never "absent": it is
# reported on stderr and the function fails, as for an item that exists.
scratch_item_absent() {
  local rc=0
  /usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a "$1" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 1 ;;
    44) return 0 ;;
    *)
      printf 'error: checking the scratch Keychain item %s failed (security exit %s)\n' "$1" "$rc" >&2
      return 1
      ;;
  esac
}

delete_scratch_items() {
  local account
  for account in "${ACCOUNTS[@]}"; do
    /usr/bin/security delete-generic-password -s "$SCRATCH_SERVICE" -a "$account" >/dev/null 2>&1 || true
  done
}

# The pid of the running scratch daemon (the holder of the scratch state's lock), or
# nothing.
scratch_daemon_pid() {
  [[ -f "$STATE/daemon.lock" ]] || return 0
  "$PY" -c 'import sys; from pathlib import Path; from diffbell.lock import holder_pid; print(holder_pid(Path(sys.argv[1])) or "")' "$STATE/daemon.lock"
}

# The private Watchman's pid from WM_INFO, printed only while that process runs and
# its command line names the recorded private socket; fails otherwise.
private_watchman_pid() {
  [[ -f "$WM_INFO" ]] || return 1
  local WM_DIR WM_SOCK WM_PID
  # shellcheck source=/dev/null
  source "$WM_INFO"
  kill -0 "$WM_PID" 2>/dev/null || return 1
  # -ww: the full command line, never cut at a terminal width.
  ps -ww -p "$WM_PID" -o command= | grep -qF -- "--unix-listener-path=$WM_SOCK" || return 1
  printf '%s\n' "$WM_PID"
}

# Stop the private Watchman scratch-check.sh prepare started (SIGTERM to that process;
# never asks a Watchman to shut down). Only a process whose command line names the
# recorded private socket is signalled.
stop_private_watchman() {
  [[ -f "$WM_INFO" ]] || return 0
  local WM_DIR WM_SOCK WM_PID
  # shellcheck source=/dev/null
  source "$WM_INFO"
  kill -0 "$WM_PID" 2>/dev/null || return 0
  # -ww: the full command line, never cut at a terminal width.
  if ps -ww -p "$WM_PID" -o command= | grep -qF -- "--unix-listener-path=$WM_SOCK"; then
    kill -TERM "$WM_PID"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$WM_PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$WM_PID" 2>/dev/null; then kill -KILL "$WM_PID"; fi
    echo "Stopped the private Watchman (pid $WM_PID)."
  else
    echo "pid $WM_PID is not the private Watchman any more; left alone."
  fi
}
