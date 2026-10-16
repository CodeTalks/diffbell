#!/usr/bin/env bash
# Live check A (bot onboarding spec §4): run the branch's daemon not set up, then the
# wizard, against scratch config/state/logs, scratch Keychain items and a private
# Watchman. Never runs install, never uses the live Keychain service, never talks to
# the default Watchman socket.
#   scripts/live/scratch-check.sh prepare | steps | touch | leakcheck | forget | marker
set -euo pipefail
# shellcheck source=scripts/live/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

prepare() {
  require_venv
  command -v watchman >/dev/null || die "watchman is not on PATH (brew install watchman)"
  [[ ! -e "$SCRATCH_ROOT" ]] || die "$SCRATCH_ROOT exists: run scripts/live/scratch-cleanup.sh first"
  local account
  for account in "${ACCOUNTS[@]}"; do
    if ! scratch_item_absent "$account"; then
      die "a scratch Keychain item ($account) exists or can't be checked: run scripts/live/scratch-cleanup.sh first"
    fi
  done
  if lsof -nP -iTCP:"$SCRATCH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $SCRATCH_PORT is in use; free it first"
  fi

  # One level at a time: `mkdir -m 700 -p` leaves the intermediate dirs at the umask.
  mkdir -p "$(dirname "$SCRATCH_ROOT")"
  local dir
  for dir in "$SCRATCH_ROOT" "$SCRATCH_ROOT/config" "$(dirname "$CONFIG")" \
    "$STATE" "$LOGS" "$WM_BASE" "$PROJECT"; do
    mkdir -m 700 "$dir"
  done

  cat >"$CONFIG" <<EOF
# Scratch config for live check A (scripts/live). Never the live one.
web:
  enabled: true
  port: $SCRATCH_PORT
notify:
  heartbeat: "09:00"
projects:
  - path: "$PROJECT"
    name: scratch
EOF
  chmod 600 "$CONFIG"

  printf '{\n  "name": "scratch",\n  "version": "0.0.0"\n}\n' >"$PROJECT/package.json"
  git -C "$PROJECT" init -q
  git -C "$PROJECT" add package.json
  git -C "$PROJECT" -c user.name=scratch -c user.email=scratch@example.invalid commit -q -m baseline

  # The user's `tee` (step 1) keeps this file's mode: create it 0600 first.
  : >"$LOGS/run-stderr.log"
  chmod 600 "$LOGS/run-stderr.log"

  # The test suite's private Watchman (ADR 0014): its own socket, state and log in a
  # fresh folder under WM_BASE; started from the repo so `tests` is importable. If
  # prepare fails after it started, the EXIT trap stops it again (its pid is in
  # WM_INFO; the Python stops it itself if writing WM_INFO fails).
  trap stop_private_watchman EXIT
  (cd "$REPO" && "$PY" - "$WM_BASE" "$WM_INFO" <<'EOF'
import shlex
import sys
from pathlib import Path

from tests.private_watchman import start_private

private = start_private(base=Path(sys.argv[1]))
try:
    Path(sys.argv[2]).write_text(
        f"WM_DIR={shlex.quote(str(private.directory))}\n"
        f"WM_SOCK={shlex.quote(str(private.sock))}\n"
        f"WM_PID={private.proc.pid}\n"
    )
    Path(sys.argv[2]).chmod(0o600)
except BaseException:
    private.stop()
    raise
EOF
  )
  # shellcheck source=/dev/null
  source "$WM_INFO"
  printf 'Prepared %s\n  private Watchman pid %s, socket %s\n  web UI port %s\n\n' \
    "$SCRATCH_ROOT" "$WM_PID" "$WM_SOCK" "$SCRATCH_PORT"
  trap - EXIT # prepared: the private Watchman keeps running for the steps
  steps
}

steps() {
  cat <<'EOF'
Live check A: run each step yourself, in order. "dbell" is scripts/live/dbell.

 1. Terminal 1:  scripts/live/dbell run -v 2>&1 | tee ~/.cache/diffbell-scratch/logs/run-stderr.log
    Expect: the daemon starts (no exit 2) and logs "Telegram not set up. Watching
    and holding changes until it is set up (Settings → Telegram, or `diffbell
    setup`)", then connects to the private Watchman.
 2. Terminal 2:  scripts/live/dbell status
    Expect: "telegram: not set up (Settings → Telegram, or `diffbell setup`)".
    scripts/live/dbell doctor
    Expect: Keychain WARN "not set up", Telegram WARN "not set up, nothing to check",
    Open problems WARN (the Telegram setup warning). Install, launchd agent and
    Watchman describe your installed Diffbell and your own Watchman: doctor can't see
    the scratch ones. Ignore those rows (exit 0 unless one of them fails).
    scripts/live/dbell test
    Expect: exit 2, "Telegram isn't set up: use Settings → Telegram in the web UI
    (`diffbell web`), or run `diffbell setup`."
 3. scripts/live/scratch-check.sh touch      (changes package.json in the scratch repo)
    Wait ~15 s (10 s debounce).
 4. scripts/live/dbell web
    Expect: the browser opens http://127.0.0.1:8799 logged in. The dashboard banner
    says "Telegram isn't set up — 1 change is waiting." with "Set up ▸"; the event
    is Held; the telegram.setup warning is listed; Settings → Telegram shows the
    wizard with "1 change is waiting and will arrive as one message per project."
 5. Click "Set up ▸": the URL is ?view=settings&section=telegram, the Telegram card
    is open on step 1. Click "How? ▸": ?view=guide&step=create; reload keeps the
    step; "← Back to Settings" returns to the card.
 6. In Telegram, @BotFather → /newbot → a THROWAWAY bot (never your live bot).
    Step 2: type "abc" → Check → "That doesn't look like a bot token."
    Paste the real token → Check → the field clears and @yourbot shows.
 7. Step 3: open the bot in Telegram and send /start. Within ~3 s your chat
    appears by name, preselected.
 8. Step 4: "Send test message" → the sample notification arrives (no "🔗 Open"
    line). Save becomes enabled.
 9. Save → the card says "Notifications go to <your name> via @yourbot".
    Within ~5 s ONE catch-up arrives for "scratch" whose header says "held until
    Telegram was set up". The banner and the telegram.setup warning are gone. No
    ⚠️/✅ backlog arrives, and no heartbeat arrives right after the save (even after
    09:00).
10. scripts/live/dbell status → "telegram: @yourbot ok".
    scripts/live/dbell doctor → Keychain OK, Telegram OK.
    scripts/live/dbell test   → a sample notification arrives.
11. scripts/live/scratch-check.sh touch → a normal notification ending in
    "🔗 Open #N" that opens http://127.0.0.1:8799/?event=N.
12. scripts/live/scratch-check.sh leakcheck → "no token or chat id found".

Optional:
A2 Terminal writer + restart hint: scripts/live/scratch-check.sh forget, then
   restart terminal 1 (Ctrl-C, step 1 again) → not set up again. Then
   scripts/live/dbell setup with the same throwaway bot → it prints "Saved; the running
   Diffbell keeps its previous bot (or none) until you run `diffbell install`."
   scripts/live/dbell status → under the telegram line: "saved, but the running
   Diffbell started before that: run `diffbell install`".
   Do NOT run install here: restart terminal 1 instead → status "@yourbot ok".
A3 Unfinished save: stop terminal 1, scripts/live/scratch-check.sh marker, start
   terminal 1 again → not set up, "not set up: an earlier save didn't finish"
   (dbell status, dbell doctor's Keychain row; on the page the card reads "An earlier
   save didn't finish" with "Set up again"). Saving in the wizard clears it.
A4 Replace bot: on the Connected card, "Replace bot" with a second throwaway bot;
   the first keeps sending until Save; afterwards dbell test arrives from the second.

Settings view and Telegram card states (live check S): scripts/live/settings-check.sh.

Cleanup: Ctrl-C terminal 1, then scripts/live/scratch-cleanup.sh. Delete the
throwaway bot(s) in @BotFather (/deletebot).
EOF
}

touch_project() {
  [[ -d "$PROJECT/.git" ]] || die "no scratch project: run prepare first"
  printf '{\n  "name": "scratch",\n  "version": "0.0.%s"\n}\n' "$(date +%s)" >"$PROJECT/package.json"
  echo "Changed $PROJECT/package.json"
}

leakcheck() {
  local token chat hits=""
  token="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a bot-token -w 2>/dev/null)" ||
    die "no scratch token saved yet"
  chat="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a chat-id -w 2>/dev/null)" ||
    die "no scratch chat id saved yet"
  # Patterns go to grep through a file descriptor, never argv; nothing is printed.
  hits="$(grep -rFla -f <(printf '%s\n' "${token#*:}") "$SCRATCH_ROOT" 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    printf 'LEAK: the bot token was found in:\n%s\n' "$hits"
    exit 1
  fi
  hits="$(grep -rFlw -f <(printf '%s\n' "$chat") "$LOGS" 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    printf 'LEAK: the chat id was found in the logs:\n%s\n' "$hits"
    exit 1
  fi
  echo "leakcheck: no token or chat id found under $SCRATCH_ROOT (chat id: logs only)"
}

forget() {
  local account
  for account in bot-token chat-id save-pending; do
    /usr/bin/security delete-generic-password -s "$SCRATCH_SERVICE" -a "$account" >/dev/null 2>&1 || true
  done
  echo "Deleted the scratch bot-token, chat-id and save-pending items (if any)."
}

marker() {
  require_venv
  DIFFBELL_KEYCHAIN_SERVICE="$SCRATCH_SERVICE" "$PY" -c \
    'import time; from diffbell.secrets import set_secret; set_secret("save-pending", str(time.time()))'
  echo "Created the scratch save-pending marker."
}

case "${1:-steps}" in
prepare) prepare ;;
steps) steps ;;
touch) touch_project ;;
leakcheck) leakcheck ;;
forget) forget ;;
marker) marker ;;
*) die "usage: $0 prepare | steps | touch | leakcheck | forget | marker" ;;
esac
