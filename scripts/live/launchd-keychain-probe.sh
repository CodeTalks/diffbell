#!/usr/bin/env bash
# Live check B (bot onboarding spec §4, ADR 0003, ADR 0018): can a process started by
# launchd update and delete Keychain items the terminal created, with no access prompt?
#  1. the terminal creates scratch items (set_secret, service io.688.diffbell-scratch):
#     probe, chat-id, bot-token and a leftover save-pending marker;
#  2. a temporary agent (label io.688.diffbell-scratch, ProcessType Standard, PATH
#     as the real plist, plus DIFFBELL_KEYCHAIN_SERVICE) runs set_secret("probe")
#     and save_credentials(...) from the worktree's .venv;
#  3. the terminal reads the new values back;
#  4. bootout, the plist and the scratch items are removed (also on failure).
# The plist lives in a temporary folder under $TMPDIR, never in your launch agents
# folder. Never touches the live agent, plist or Keychain items. Run it yourself:
#   scripts/live/launchd-keychain-probe.sh
set -euo pipefail
# shellcheck source=scripts/live/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_venv

uid="$(id -u)"
domain="gui/$uid"
if launchctl print "$domain/$SCRATCH_LABEL" >/dev/null 2>&1; then
  die "$SCRATCH_LABEL is already loaded: launchctl bootout $domain/$SCRATCH_LABEL"
fi
for account in "${ACCOUNTS[@]}"; do
  if ! scratch_item_absent "$account"; then
    die "a scratch Keychain item ($account) exists or can't be checked: run scripts/live/scratch-cleanup.sh first"
  fi
done

work="$(mktemp -d "${TMPDIR:-/tmp}/df-probe.XXXXXX")"
plist="$work/$SCRATCH_LABEL.plist"
cleanup() {
  # Every step runs, whatever fails: errexit would cut the leftover checks short.
  set +e
  local warned=0 account
  launchctl bootout "$domain/$SCRATCH_LABEL" >/dev/null 2>&1
  delete_scratch_items
  if ! rm -rf "$work"; then
    echo "WARNING: $work could not be removed completely" >&2
    warned=1
  fi
  if launchctl print "$domain/$SCRATCH_LABEL" >/dev/null 2>&1; then
    echo "WARNING: $SCRATCH_LABEL is still loaded: launchctl bootout $domain/$SCRATCH_LABEL" >&2
    warned=1
  fi
  for account in "${ACCOUNTS[@]}"; do
    if ! scratch_item_absent "$account"; then
      echo "WARNING: the scratch Keychain item $account is still there (or can't be checked): run scripts/live/scratch-cleanup.sh" >&2
      warned=1
    fi
  done
  if [[ "$warned" == 0 ]]; then
    echo "Cleaned up: label booted out, plist and scratch Keychain items removed."
  else
    echo "Cleanup incomplete: see the warnings above." >&2
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

nonce="$(date +%s)-$$"
agent_value="agent-$nonce"

echo "1. The terminal creates the scratch items."
DIFFBELL_KEYCHAIN_SERVICE="$SCRATCH_SERVICE" "$PY" - "terminal-$nonce" <<'EOF'
import sys

from diffbell.secrets import service, set_secret

assert service() == "io.688.diffbell-scratch", service()
set_secret("probe", sys.argv[1])
set_secret("chat-id", "1")
set_secret("bot-token", "1:terminal-token-" + sys.argv[1])
set_secret("save-pending", "left-by-the-terminal")  # an earlier save that didn't finish
EOF

echo "2. A scratch agent ($SCRATCH_LABEL) updates and deletes them."
"$PY" - "$plist" "$SCRATCH_LABEL" "$PY" "$SCRATCH_SERVICE" "$agent_value" "$work" <<'EOF'
import plistlib
import sys

from diffbell.launchd import LAUNCHD_PATH

plist, label, python, service, value, work = sys.argv[1:]
code = (
    "import sys\n"
    "from diffbell.secrets import save_credentials, service, set_secret\n"
    "try:\n"
    "    assert service() == 'io.688.diffbell-scratch', service()\n"
    "    set_secret('probe', sys.argv[1])\n"
    "    save_credentials('2:agent-token-' + sys.argv[1], '2')\n"
    "except Exception as exc:\n"
    "    with open(sys.argv[2], 'w') as out:\n"
    "        out.write(f'error {type(exc).__name__}: {exc}\\n')\n"
    "    raise SystemExit(1)\n"
    "with open(sys.argv[2], 'w') as out:\n"
    "    out.write('ok\\n')\n"
)
with open(plist, "wb") as handle:
    plistlib.dump(
        {
            "Label": label,
            "ProgramArguments": [python, "-c", code, value, f"{work}/result"],
            "EnvironmentVariables": {
                "PATH": LAUNCHD_PATH,
                "DIFFBELL_KEYCHAIN_SERVICE": service,
            },
            "WorkingDirectory": work,
            "Umask": 0o077,
            "ProcessType": "Standard",
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": f"{work}/out.log",
            "StandardErrorPath": f"{work}/out.log",
        },
        handle,
    )
EOF
plutil -lint "$plist" >/dev/null || die "the temporary plist doesn't lint"
launchctl bootstrap "$domain" "$plist" || die "launchctl bootstrap $domain failed"

for _ in $(seq 1 120); do
  [[ -s "$work/result" ]] && break
  sleep 0.5
done
if [[ ! -s "$work/result" ]]; then
  cat "$work/out.log" 2>/dev/null || true
  die "FAIL: the agent wrote no result in 60 s (a Keychain access dialog on screen? that is the failure this probe looks for)"
fi
result="$(cat "$work/result")"
if [[ "$result" != "ok" ]]; then
  cat "$work/out.log" 2>/dev/null || true
  die "FAIL: the agent reported: $result"
fi

echo "3. The terminal reads the values back."
got="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a probe -w)" ||
  die "FAIL: the probe item can't be read back"
[[ "$got" == "$agent_value" ]] || die "FAIL: the probe item still has the terminal's value"
got="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a chat-id -w)" ||
  die "FAIL: chat-id can't be read back"
[[ "$got" == "2" ]] || die "FAIL: chat-id was not updated by the agent"
got="$(/usr/bin/security find-generic-password -s "$SCRATCH_SERVICE" -a bot-token -w)" ||
  die "FAIL: bot-token can't be read back"
[[ "$got" == "2:agent-token-$agent_value" ]] || die "FAIL: bot-token was not updated by the agent"
if ! scratch_item_absent save-pending; then
  die "FAIL: the agent did not delete the terminal's save-pending marker (or it can't be checked)"
fi

echo "PASS: the launchd agent updated probe, chat-id and bot-token and deleted"
echo "      save-pending, all created by the terminal. If macOS showed ANY Keychain"
echo "      access dialog during this run, report it: the check failed even so."
