#!/usr/bin/env bash
# Undo live check A: stop the private Watchman (SIGTERM to the process prepare
# started; it never asks a Watchman to shut down), delete the scratch Keychain items,
# remove the scratch folder. Refuses while the scratch daemon still runs.
set -euo pipefail
# shellcheck source=scripts/live/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_venv

holder="$(scratch_daemon_pid)"
[[ -z "$holder" ]] || die "the scratch daemon (pid $holder) still runs: Ctrl-C \`scripts/live/dbell run\` first"

stop_private_watchman

delete_scratch_items
for account in "${ACCOUNTS[@]}"; do
  if ! scratch_item_absent "$account"; then
    die "the scratch Keychain item $account is still there (or can't be checked)"
  fi
done
echo "Deleted the scratch Keychain items ($SCRATCH_SERVICE)."

rm -rf "$SCRATCH_ROOT"
echo "Removed $SCRATCH_ROOT."
echo "Optional: delete the throwaway bot(s) in @BotFather (/deletebot)."
