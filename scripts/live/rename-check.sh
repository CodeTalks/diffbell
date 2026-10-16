#!/usr/bin/env bash
# Live check R (rename spec §7): build an OLD-named scratch install (no agent, the
# dev-watchman scratch Keychain service, the old folder names), move it with the new
# code (python -m diffbell.rename_install) and check the result. Never the live
# install, the live Keychain services or the default Watchman socket; verify and
# conflict also check that the real old and new install paths were not touched.
#   scripts/live/rename-check.sh prepare | move | verify | conflict | cleanup | steps
set -euo pipefail
# shellcheck source=scripts/live/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# The layout OldInstall.scratch(OLD_ROOT) expects (src/diffbell/rename_install.py).
OLD_ROOT="$HOME/.cache/dev-watchman-scratch-old"
OLD_LABEL="io.688.dev-watchman-scratch" # the label and the Keychain service
OLD_SERVICE="$OLD_LABEL"
OLD_CONFIG_DIR="$OLD_ROOT/config/dev-watchman"
OLD_STATE="$OLD_ROOT/state"
OLD_LOGS="$OLD_ROOT/logs"
OLD_PLIST="$OLD_ROOT/LaunchAgents/$OLD_LABEL.plist" # never created: no scratch agent
OLD_ASIDE="$OLD_ROOT/$OLD_LABEL.plist.moving"
MARKER="$OLD_ROOT/.diffbell-rename-scratch" # rename_install.SCRATCH_MARKER
OLD_ACCOUNTS=(bot-token chat-id save-pending)
OLD_CHECKOUT="$OLD_ROOT/projects/dev-watchman"
# main's live check A uses the same io.688.dev-watchman-scratch service.
MAIN_SCRATCH_ROOT="$HOME/.cache/dev-watchman-scratch" # old-name
NEW_CHECKOUT="$OLD_ROOT/projects/diffbell"
ID_FILE="$SCRATCH_ROOT/seeded-project-id"
REAL_BEFORE="$SCRATCH_ROOT/real-before.txt"
# Dummies, not secrets: still never printed and never put in argv.
TOKEN="123456:scratch-token-not-real"
CHAT="4242"

[[ "$OLD_ROOT" == /* && "$OLD_ROOT" == "$HOME/.cache/"* ]] || die "unexpected old scratch root"
[[ "$OLD_SERVICE" == "io.688.dev-watchman-scratch" ]] || die "unexpected old scratch service"

# Write a Keychain item of the OLD scratch service, the value read from stdin. Run
# with no controlling terminal (as secrets._run does): otherwise `security ... -w`
# reads the password from /dev/tty, not from the pipe.
put_old_item() {
  "$PY" -I -c '
import subprocess, sys
value = sys.stdin.read()
subprocess.run(
    ["/usr/bin/security", "add-generic-password", "-U", "-s", sys.argv[1], "-a", sys.argv[2], "-w"],
    input=f"{value}\n{value}\n", text=True, capture_output=True, check=True,
    start_new_session=True,
)' "$OLD_SERVICE" "$1"
}

old_item_absent() {
  local rc=0
  /usr/bin/security find-generic-password -s "$OLD_SERVICE" -a "$1" >/dev/null 2>&1 || rc=$?
  [[ $rc == 44 ]]
}

old_item_present() {
  /usr/bin/security find-generic-password -s "$OLD_SERVICE" -a "$1" >/dev/null 2>&1
}

# Whether the new scratch item holds $2: compared here, the value never printed.
new_item_is() {
  local value
  value="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a "$1" -w 2>/dev/null)" || return 1
  [[ "$value" == "$2" ]]
}

# The real install's paths and Keychain items (from the login home, as rename_install does), each with
# its inode or "absent". BSD stat -f does not follow a symlink.
real_fingerprint() {
  local home path ino service account rc
  home="$("$PY" -I -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
  for path in \
    "$home/.config/dev-watchman" \
    "$home/.local/share/dev-watchman" \
    "$home/Library/Logs/dev-watchman" \
    "$home/Library/LaunchAgents/io.688.dev-watchman.plist" \
    "$home/.config/diffbell" \
    "$home/.local/share/diffbell" \
    "$home/Library/Logs/diffbell" \
    "$home/Library/LaunchAgents/io.688.diffbell.plist" \
    "$home/Library/io.688.dev-watchman.plist.moving" \
    "$home/Library/io.688.dev-watchman-scratch.plist.moving"; do
    if [[ -e "$path" || -L "$path" ]]; then
      ino="$(stat -f %i "$path" 2>/dev/null)" || ino=unreadable
      printf '%s %s\n' "$ino" "$path"
    else
      printf 'absent %s\n' "$path"
    fi
  done
  # The real Keychain items: existence only (the exit code), never a value.
  for service in io.688.dev-watchman io.688.diffbell; do
    for account in bot-token chat-id save-pending; do
      rc=0
      /usr/bin/security find-generic-password -s "$service" -a "$account" >/dev/null 2>&1 || rc=$?
      printf '%s/%s %s\n' "$service" "$account" "$rc"
    done
  done
}

real_untouched() {
  [[ -f "$REAL_BEFORE" ]] || return 1
  diff -u "$REAL_BEFORE" <(real_fingerprint) >&2
}

no_main_live_check_a() {
  if [[ -e "$MAIN_SCRATCH_ROOT" || -L "$MAIN_SCRATCH_ROOT" ]]; then
    die "a dev-watchman live-check-A scratch install exists; run its scratch-cleanup.sh from a main checkout first"
  fi
}

prepare() {
  require_venv
  no_main_live_check_a
  [[ ! -e "$OLD_ROOT" && ! -L "$OLD_ROOT" && ! -e "$SCRATCH_ROOT" && ! -L "$SCRATCH_ROOT" ]] || die "run cleanup first ($OLD_ROOT or $SCRATCH_ROOT exists)"
  local account
  for account in "${OLD_ACCOUNTS[@]}"; do
    old_item_absent "$account" || die "an old scratch Keychain item ($OLD_SERVICE/$account) exists or can't be checked: it may belong to main's live check A (run its scratch-cleanup.sh from a main checkout), else run cleanup first"
  done
  for account in "${ACCOUNTS[@]}"; do
    scratch_item_absent "$account" || die "a scratch Keychain item ($account) exists or can't be checked: run cleanup first"
  done

  # One level at a time: `mkdir -m 700 -p` leaves the intermediate dirs at the umask.
  mkdir -p "$(dirname "$OLD_ROOT")"
  local dir
  for dir in "$OLD_ROOT" "$OLD_ROOT/config" "$OLD_CONFIG_DIR" "$OLD_STATE" "$OLD_LOGS" \
    "$OLD_ROOT/projects" "$NEW_CHECKOUT"; do
    mkdir -m 700 "$dir"
  done
  : >"$MARKER"
  # The recorded checkout is gone: it "moved" to its sibling named diffbell.
  printf '[project]\nname = "diffbell"\n' >"$NEW_CHECKOUT/pyproject.toml"
  cat >"$OLD_CONFIG_DIR/config.yaml" <<EOF
web:
  enabled: false
projects:
  - path: "$OLD_CHECKOUT"
    name: repo
EOF
  chmod 600 "$OLD_CONFIG_DIR/config.yaml"
  printf 'old log line\n' >"$OLD_LOGS/dev-watchman.log"

  # Only the root: the move creates config/, state and logs under it.
  mkdir -m 700 "$SCRATCH_ROOT"
  real_fingerprint >"$REAL_BEFORE"
  # The seeded project's id goes outside the folders the move takes, so verify can
  # check the row at the new path is the same row (moved, not recreated).
  "$PY" -I - "$OLD_STATE" "$OLD_CHECKOUT" "$ID_FILE" <<'EOF'
import sys
from pathlib import Path
from diffbell.install_source import Source, write_source
from diffbell.store import Store
state, checkout, id_file = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
write_source(state, Source(checkout, "abc1234", "main", 1.0))
store = Store(state / "state.db")
store.sync_projects([("repo", checkout)], 1.0)
pid = store.project_token("repo")
store.close()
id_file.write_text(str(pid))
print(f"seeded project id {pid}")
EOF
  printf '%s' "$TOKEN" | put_old_item bot-token
  printf '%s' "$CHAT" | put_old_item chat-id
  echo "Prepared the old-named install in $OLD_ROOT (dummy credentials under $OLD_SERVICE)."
}

move() {
  require_venv
  [[ -f "$MARKER" && -d "$SCRATCH_ROOT" ]] || die "nothing prepared: run prepare first"
  (
    export DIFFBELL_CONFIG="$CONFIG"
    export DIFFBELL_STATE_DIR="$STATE"
    export DIFFBELL_LOG_DIR="$LOGS"
    export DIFFBELL_KEYCHAIN_SERVICE="$SCRATCH_SERVICE"
    export DIFFBELL_RENAME_FROM="$OLD_ROOT"
    # A dead socket: the move doesn't use Watchman, and nothing may reach the user's.
    export WATCHMAN_SOCK="$SCRATCH_ROOT/no-watchman.sock"
    # The move runs with the caller's umask, as dbell does, not the scripts' 077.
    umask "$ORIG_UMASK"
    exec "$PY" -I -m diffbell.rename_install
  )
}

# The same row moved: the seeded id is at the new path and nothing is left at the old.
project_moved() {
  [[ -f "$ID_FILE" && -f "$STATE/state.db" ]] || return 1
  "$PY" -I - "$STATE/state.db" "$NEW_CHECKOUT" "$OLD_CHECKOUT" "$(cat "$ID_FILE")" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
def at(path): return [row[0] for row in db.execute("SELECT id FROM projects WHERE path = ?", (path,))]
sys.exit(0 if at(sys.argv[2]) == [int(sys.argv[4])] and at(sys.argv[3]) == [] else 1)
EOF
}

old_folders_gone() { [[ ! -e "$OLD_CONFIG_DIR" && ! -L "$OLD_CONFIG_DIR" && ! -e "$OLD_STATE" && ! -L "$OLD_STATE" && ! -e "$OLD_LOGS" && ! -L "$OLD_LOGS" ]]; }
old_folders_in_place() { [[ -f "$OLD_CONFIG_DIR/config.yaml" && -f "$OLD_STATE/state.db" && -f "$OLD_LOGS/dev-watchman.log" ]]; }
log_renamed() { [[ -f "$LOGS/diffbell.log" && ! -e "$LOGS/dev-watchman.log" && ! -L "$LOGS/dev-watchman.log" ]]; }
private_and_ours() { [[ -d "$1" && ! -L "$1" && "$(stat -f %Lp "$1")" == 700 && "$(stat -f %u "$1")" == "$(id -u)" ]]; }
old_items_gone() {
  local account
  for account in "${OLD_ACCOUNTS[@]}"; do old_item_absent "$account" || return 1; done
}
old_items_kept() { old_item_present bot-token && old_item_present chat-id; }
config_follows() { grep -qF "$NEW_CHECKOUT" "$CONFIG" && ! grep -qF "$OLD_CHECKOUT" "$CONFIG"; }
source_follows() { grep -qF "\"$NEW_CHECKOUT\"" "$STATE/install-source.json"; }
no_aside() { [[ ! -e "$OLD_ASIDE" && ! -L "$OLD_ASIDE" && ! -e "$OLD_PLIST" && ! -L "$OLD_PLIST" ]]; }

OK=1
check() {
  local what="$1"
  shift
  if "$@"; then echo "ok    $what"; else
    echo "FAIL  $what"
    OK=0
  fi
}

verify() {
  require_venv
  check "old folders gone" old_folders_gone
  check "new config moved" test -f "$CONFIG"
  check "log renamed" log_renamed
  # Mode and owner kept for all three moved folders (spec §3 step 2).
  local dir
  for dir in "$(dirname "$CONFIG")" "$STATE" "$LOGS"; do
    check "$dir is 0700 and ours" private_and_ours "$dir"
  done
  check "token copied" new_item_is bot-token "$TOKEN"
  check "chat id copied" new_item_is chat-id "$CHAT"
  check "no new save-pending marker" scratch_item_absent save-pending
  check "old scratch items gone" old_items_gone
  check "config follows the checkout" config_follows
  check "project moved with its id" project_moved
  check "install-source follows" source_follows
  check "no plist or aside left in the old root" no_aside
  check "marker still there (the root isn't moved)" test -f "$MARKER"
  check "real install untouched" real_untouched
  [[ $OK == 1 ]] || die "verify failed"
}

conflict() {
  # A non-empty new state folder next to an old one: refused, nothing moved.
  require_venv
  prepare
  mkdir -p "$STATE"
  echo x >"$STATE/x"
  local out
  if out="$(move 2>&1)"; then
    printf '%s\n' "$out"
    die "the move should have been refused"
  fi
  printf '%s\n' "$out"
  grep -qF 'both exist; move or remove one' <<<"$out" || die "refused for another reason"
  check "the new state folder's file is still there" test -f "$STATE/x"
  check "no new scratch bot-token" scratch_item_absent bot-token
  check "the old folders are all in place" old_folders_in_place
  check "no new config" test ! -e "$CONFIG"
  check "the old scratch items are kept" old_items_kept
  check "no plist or aside left in the old root" no_aside
  check "real install untouched" real_untouched
  [[ $OK == 1 ]] || die "conflict check failed"
  echo "ok    a non-empty new folder is refused; the old install is untouched"
}

cleanup() {
  require_venv
  # Never while a scratch daemon runs: scratch-cleanup.sh refuses then, and nothing
  # here may delete a running daemon's state folder.
  local holder account
  no_main_live_check_a
  holder="$(scratch_daemon_pid)"
  [[ -z "$holder" ]] || die "the scratch daemon (pid $holder) still runs: stop \`scripts/live/dbell run\` first"
  for account in "${OLD_ACCOUNTS[@]}"; do
    /usr/bin/security delete-generic-password -s "$OLD_SERVICE" -a "$account" >/dev/null 2>&1 || true
  done
  for account in "${OLD_ACCOUNTS[@]}"; do
    old_item_absent "$account" || die "the old scratch Keychain item $account is still there (or can't be checked)"
  done
  rm -rf "$OLD_ROOT"
  echo "Removed $OLD_ROOT and the old scratch Keychain items ($OLD_SERVICE)."
  # scratch-cleanup.sh also handles a scratch root whose private Watchman never
  # started (no watchman.env): stop_private_watchman then does nothing.
  if [[ -e "$SCRATCH_ROOT" || -L "$SCRATCH_ROOT" ]]; then
    "$LIVE_DIR/scratch-cleanup.sh"
  fi
}

steps() {
  cat <<'EOF'
Live check R (scratch only; never the live install):
  R1  scripts/live/rename-check.sh prepare   -> "Prepared the old-named install ..."
  R2  scripts/live/rename-check.sh move      -> the step lines, then
      "Moved from dev-watchman. Remove the old tool: ..."
  R3  scripts/live/rename-check.sh verify    -> every line "ok", none "FAIL"
      (the last one: "real install untouched")
  R4  scripts/live/rename-check.sh move      -> again: nothing printed (nothing left
      to move), exit 0
  R5  scripts/live/rename-check.sh cleanup, then
      scripts/live/rename-check.sh conflict  -> "ok    a non-empty new folder is
      refused; the old install is untouched", then
      scripts/live/rename-check.sh cleanup
EOF
}

case "${1:-}" in
prepare) prepare ;;
move) move ;;
verify) verify ;;
conflict) conflict ;;
cleanup) cleanup ;;
steps) steps ;;
*) die "usage: $0 prepare | move | verify | conflict | cleanup | steps" ;;
esac
