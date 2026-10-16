#!/usr/bin/env bash
# Live check S (settings redesign spec §9): the Telegram card's states and the save
# bar, on the scratch setup of live check A (scratch-check.sh prepare, then its steps
# 1-10). Scratch config, state, logs and Keychain items and the private Watchman
# only: never the live config, state, Keychain items, launchd agent or the default
# Watchman socket.
#   scripts/live/settings-check.sh steps | handedit | breakconfig | fixconfig
#     | reject-setup | reject | reject-undo | badchat | legacy
set -euo pipefail
# shellcheck source=scripts/live/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ALT="$SCRATCH_ROOT/alt"
ALT_NAME="scratch-alt"
INVALID_LINE="live_check_invalid: true"
PAUSED_WM=""

require_prepared() {
  require_venv
  [[ -f "$WM_INFO" && -f "$CONFIG" && -d "$PROJECT/.git" ]] ||
    die "no scratch setup: run scripts/live/scratch-check.sh prepare first"
}

require_daemon_running() {
  [[ -n "$(scratch_daemon_pid)" ]] || die "start the scratch daemon first (terminal 1: scripts/live/dbell run -v)"
}

require_daemon_stopped() {
  local pid
  pid="$(scratch_daemon_pid)"
  [[ -z "$pid" ]] || die "stop the scratch daemon first (pid $pid: Ctrl-C terminal 1)"
}

# A hand edit appends lines: make sure the file ends with a newline first.
end_with_newline() {
  if [[ -s "$CONFIG" && -n "$(tail -c 1 "$CONFIG")" ]]; then
    printf '\n' >>"$CONFIG"
  fi
}

steps() {
  cat <<'EOF'
Live check S: run each step yourself, in order. "dbell" is scripts/live/dbell, "sc" is
scripts/live/scratch-check.sh, "st" is scripts/live/settings-check.sh.
Before: live check A steps 1-10 are done (terminal 1 runs the scratch daemon, a
throwaway bot is saved, the page is open at http://127.0.0.1:8799).

Part 1: the view and the save bar
 S1 Open http://127.0.0.1:8799/?view=settings&section=schedule
    Expect: the index lists Telegram · Notifications · Schedule · Patterns · This
    page; the Schedule heading has focus. ?view=settings&section=nope opens the view
    at the top. The header's theme button cycles System → Light → Dark and This page
    → Theme follows it (and the other way round). This page → Theme → Dark: the page
    turns dark at once; reload: still dark, no light flash; System follows macOS.
 S2 Notifications → "Wait for quiet": 10 → 12.
    Expect: the bar shows "1 unsaved change" with Discard and Save changes.
    "Diff lines per file": 20 → 30 → "2 unsaved changes".
    "Wait for quiet": abc → about 0.3 s later "Enter a number of seconds." under the
    field and "2 unsaved changes · fix the highlighted fields to save"; Save
    changes is disabled and clicking it focuses the field. Empty the field → "Enter
    a number of seconds." (never taken as 0). 2.5 → valid. "Never wait longer
    than": 1 → "Must be at least the quiet time (2.5 seconds)." Patterns → Extra
    redactions: a line "[" → a gutter mark and "line 1: …" below the list.
    Discard → every field shows the saved value again and the bar hides.
 S3 Schedule: switch Quiet hours on.
    Expect: From / To appear with a preview of the edited window, before any save
    ("Once saved: not active now · starts HH:MM" or "Once saved: active now · ends
    HH:MM"; it follows the times as you change them); switch it off and
    on again: the times you set are kept. Daily heartbeat shows "Next: today 09:00"
    or "Next: tomorrow 09:00". Discard.
 S4 Applied: "Wait for quiet" → 12, Save changes.
    Expect: "Saving…", then within ~5 s "Saved and applied by the daemon.", gone 4 s
    later. (If the daemon doesn't confirm in time the bar says "Saved: the daemon
    reloads the config." instead: the save still went through.) grep quiet_seconds ~/.cache/diffbell-scratch/config/diffbell/config.yaml
    → quiet_seconds: 12.
 S5 Conflict: change "Diff lines per file" (don't save), then: st handedit
    Save changes → "Config changed on disk. Reload to load it (your unsaved changes
    here are discarded)." with Reload and Discard. Reload → the fields show the
    file's values and the bar hides.
 S6 The daemon rejects a hand edit: st breakconfig
    Expect: within ~10 s the dashboard lists the config problem ("config rejected,
    keeping the last good config: …", naming live_check_invalid) and a ⚠️ arrives
    in Telegram. The save bar stays hidden: it reports only the page's own saves.
    Change a field and Save changes → the conflict state (the file changed); don't
    press Reload yet. st fixconfig → the problem resolves and a ✅ arrives. Now
    Reload in the bar → the fields show the file's values, the bar hides.
 S7 Rejected (the daemon rejects the version the page saved):
    st reject-setup   (adds a project scratch-alt at ~/.cache/diffbell-scratch/alt)
    Reload the page (Settings), then: st reject   and follow its prompts: change
    "Wait for quiet", press Enter (the private Watchman is paused), Save changes,
    press Enter (the alt folder becomes a symlink to the scratch project, the
    Watchman resumes).
    Expect: within ~15 s "Saved, but the daemon rejected the config (see the
    alert)."; it stays until the next edit. The dashboard lists the config problem
    ("duplicate project path: entries 1 and 2 (projects scratch and scratch-alt)"),
    a ⚠️ arrives, and scratch-alt's watch may be listed as lost. If the bar gave up
    instead (no outcome after 3 polls), the daemon applied the save before the
    swap: st reject-undo and run S7 again. Then: st reject-undo → the problem
    resolves and a ✅ arrives. Reload the page.
 S8 Change a field, open the Dashboard tab, come back to Settings: the edit and the
    bar are still there. Reload the browser tab: the browser asks before leaving.
    Discard.

Part 2: the Telegram card (Settings → Telegram)
 T1 Connected. Expect: "Notifications go to <your name> via @yourbot" · "Last sent
    …", Send test message (the sample arrives) and Replace bot in the card; under
    it "Saved separately, through its own setup steps. Setup guide".
 T2 Token rejected. In @BotFather: /revoke → your throwaway bot (Telegram issues a
    new token; keep it). sc touch, wait ~15 s.
    Expect: "Telegram rejected the bot token" · "Since HH:MM · 1 change couldn't be
    sent. The token was probably revoked in @BotFather." with Replace bot (primary)
    and Show failed changes; Show failed changes opens the dashboard at ?status=failed
    with the event and its Retry. Replace bot with the NEW token (wizard, Save).
    Expect: the card is Connected at once (cutoff rule: only errors after the
    later of the bot's save and the last successful send count, so Replace bot or
    a later successful send clears a failing card); the failed change stays in
    Show failed changes: Retry it → it arrives.
 T3 Can't reach Telegram. Ctrl-C terminal 1, then start it as:
      HTTPS_PROXY=http://127.0.0.1:9 scripts/live/dbell run -v 2>&1 | tee ~/.cache/diffbell-scratch/logs/run-stderr.log
    (only this process uses the proxy; nothing system-wide changes). sc touch, wait
    ~15 s.
    Expect: "Can't reach Telegram" · "Since HH:MM · 1 change waiting. Diffbell keeps
    retrying on its own; nothing is lost." with Send test message (it fails: Telegram
    unreachable) and Replace bot. No error text is shown in this state.
    Ctrl-C terminal 1, start it again without HTTPS_PROXY (step 1 of check A).
    Expect: within ~1 min the change arrives and the card is Connected.
 T4 Telegram refused a message. Ctrl-C terminal 1, st badchat, start terminal 1,
    sc touch, wait ~15 s.
    Expect: "Telegram refused a message" · "Since HH:MM · 1 change couldn't be
    sent." and the stored error (Telegram's "chat not found"), with Show failed
    changes and Send test message. If the card shows "Telegram rejected the bot
    token" instead, Telegram answered 403 for that chat: this state can't be
    reached this way; go on.
 T5 (optional) A failure stored before schema v7. Ctrl-C terminal 1, st legacy,
    start terminal 1.
    Expect: "Some changes couldn't be sent" · "Since HH:MM · 1 change couldn't be
    sent." with the stored error and Show failed changes.
    Precedence: Ctrl-C terminal 1, start it with HTTPS_PROXY as in T3, sc touch,
    wait ~15 s → still "Some changes couldn't be sent", never "Diffbell keeps
    retrying". Ctrl-C terminal 1, start it without HTTPS_PROXY.
 T6 Recover from T4/T5: Replace bot (offered on every failing card; the current token from T2, detect your chat
    again, Save), then Retry each failed change (Show failed changes).
    Expect: the card is Connected right after Save (cutoff rule, T2), before any
    retry; the retried changes arrive.
 T7 (optional) Name unknown: Replace bot, at the chat step type your chat id by
    hand (@userinfobot shows it), Save.
    Expect: "Notifications go to @yourbot" (no name, never the chat id).
 T8 Not set up. Ctrl-C terminal 1, sc forget, start terminal 1, sc touch, wait
    ~15 s.
    Expect: the card is the wizard with "1 change is waiting and will arrive as one
    message per project."; the dashboard banner says the same count.
 T9 An earlier save didn't finish. Ctrl-C terminal 1, sc marker, start terminal 1.
    Expect: "An earlier save didn't finish" · "Diffbell won't trust a half-saved
    bot. Changes are held until you set Telegram up again." with Set up again; it
    opens the wizard. Finish it (Save) → Connected, and one catch-up for "scratch"
    arrives.
 T10 sc leakcheck → "no token or chat id found".

Cleanup: Ctrl-C terminal 1, then scripts/live/scratch-cleanup.sh. Delete the
throwaway bot(s) in @BotFather (/deletebot).
EOF
}

handedit() {
  require_prepared
  end_with_newline
  printf '# hand edit for live check S, %s\n' "$(date +%s)" >>"$CONFIG"
  echo "Appended a comment to the scratch config: its bytes (so its config_version) changed."
}

breakconfig() {
  require_prepared
  if grep -qxF -- "$INVALID_LINE" "$CONFIG"; then
    die "the scratch config already has \"$INVALID_LINE\": run fixconfig"
  fi
  end_with_newline
  printf '%s\n' "$INVALID_LINE" >>"$CONFIG"
  echo "Added \"$INVALID_LINE\" (an unknown key) to the scratch config."
}

fixconfig() {
  require_prepared
  grep -qxF -- "$INVALID_LINE" "$CONFIG" || die "the scratch config has no \"$INVALID_LINE\" line"
  local tmp
  # Written next to the config and renamed over it (0600: common.sh's umask 077).
  tmp="$(mktemp "$(dirname "$CONFIG")/.live-check.XXXXXX")"
  grep -vxF -- "$INVALID_LINE" "$CONFIG" >"$tmp" || true
  mv -f "$tmp" "$CONFIG"
  echo "Removed \"$INVALID_LINE\" from the scratch config."
}

reject_setup() {
  require_prepared
  require_daemon_running
  [[ ! -e "$ALT" && ! -L "$ALT" ]] || die "$ALT exists: run reject-undo first"
  mkdir -m 700 "$ALT"
  "$PY" - "$CONFIG" "$ALT" "$ALT_NAME" <<'EOF'
import sys
from pathlib import Path

from diffbell.web import config_edit

config = Path(sys.argv[1])
config_edit.add_project(
    config,
    config_edit.config_version(config),
    config_edit.ProjectInput(
        path=sys.argv[2], name=sys.argv[3], patterns=[], ignore=[], use_defaults=True
    ),
)
EOF
  echo "Added the project $ALT_NAME ($ALT). Reload the page before the next step."
}

resume_watchman() {
  [[ -n "$PAUSED_WM" ]] || return 0
  kill -CONT "$PAUSED_WM" 2>/dev/null || true
  echo "Resumed the private Watchman (pid $PAUSED_WM)."
  PAUSED_WM=""
}

reject() {
  require_prepared
  require_daemon_running
  [[ -d "$ALT" && ! -L "$ALT" ]] || die "run reject-setup first (then reload the page)"
  local wm
  wm="$(private_watchman_pid)" || die "the private Watchman isn't running (or isn't the one prepare started)"
  echo "1. In the browser (Settings): change \"Wait for quiet\", don't save yet. Then press Enter here."
  read -r
  # The wait above is unbounded: check again that the pid is still the private
  # Watchman (never SIGSTOP a reused pid).
  local again
  again="$(private_watchman_pid)" || die "the private Watchman stopped meanwhile: nothing paused"
  [[ "$again" == "$wm" ]] || die "the private Watchman's pid changed ($wm → $again): nothing paused"
  PAUSED_WM="$wm"
  trap resume_watchman EXIT # never leave the private Watchman stopped
  trap 'exit 130' INT TERM HUP # an interrupt exits, so the EXIT trap resumes it
  kill -STOP "$wm"
  echo "Paused the private Watchman (pid $wm): the daemon won't see the next config write."
  echo "2. Click Save changes in the browser now, then press Enter here (within 60 s)."
  read -r -t 60 || true
  # The saved version was valid; now its second project resolves to the first one's
  # folder, so the daemon's reload of that same version fails (duplicate path).
  rmdir "$ALT"
  ln -s "$PROJECT" "$ALT"
  resume_watchman
  trap - EXIT INT TERM HUP
  # Same bytes, same config_version: only makes sure the daemon reads it again.
  touch "$CONFIG"
  echo "$ALT now points at the scratch project. Expect \"Saved, but the daemon rejected the config (see the alert).\" within ~15 s."
}

reject_undo() {
  require_prepared
  if [[ -L "$ALT" ]]; then
    rm "$ALT"
  elif [[ -d "$ALT" ]]; then
    rmdir "$ALT"
  fi
  "$PY" - "$CONFIG" "$ALT_NAME" <<'EOF'
import sys
from pathlib import Path

from diffbell.web import config_edit

config = Path(sys.argv[1])
try:
    config_edit.remove_project(config, config_edit.config_version(config), sys.argv[2])
except config_edit.ProjectNotFound:
    print(f"{sys.argv[2]} is not in the scratch config.")
else:
    print(f"Removed the project {sys.argv[2]} from the scratch config.")
EOF
}

badchat() {
  require_prepared
  require_daemon_stopped # the daemon reads the Keychain only at start
  /usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a chat-id >/dev/null 2>&1 ||
    die "no scratch chat id saved: finish live check A's wizard first"
  DIFFBELL_KEYCHAIN_SERVICE="$SCRATCH_SERVICE" "$PY" -c \
    'from diffbell.secrets import set_secret; set_secret("chat-id", "1")'
  echo "Set the scratch chat id to 1 (no such chat). T6 saves your chat again."
}

legacy() {
  require_prepared
  require_daemon_stopped
  local db="$STATE/state.db"
  [[ -f "$db" && ! -L "$db" ]] || die "no scratch database yet"
  # Only ever the scratch database: refuse anything that resolves outside the scratch root.
  local real
  real="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$db")"
  [[ "$real" == "$SCRATCH_ROOT"/state/state.db ]] || die "$db resolves to $real, not the scratch database: refusing"
  "$PY" - "$db" <<'EOF'
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1], timeout=5)
try:
    with db:
        count = db.execute(
            "UPDATE events SET last_error_kind = NULL "
            "WHERE status = 'failed' AND last_error IS NOT NULL"
        ).rowcount
finally:
    db.close()
if count == 0:
    sys.exit("no failed event with an error in the scratch database: run T4 first")
print(f"Cleared the error kind of {count} failed event(s): they read as stored before v7.")
EOF
}

case "${1:-steps}" in
steps) steps ;;
handedit) handedit ;;
breakconfig) breakconfig ;;
fixconfig) fixconfig ;;
reject-setup) reject_setup ;;
reject) reject ;;
reject-undo) reject_undo ;;
badchat) badchat ;;
legacy) legacy ;;
*) die "usage: $0 steps | handedit | breakconfig | fixconfig | reject-setup | reject | reject-undo | badchat | legacy" ;;
esac
