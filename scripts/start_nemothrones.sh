#!/usr/bin/env bash
# start_nemothrones.sh - one command to play, mirroring ~/scripts/start_hermes.sh.
#
#   ./scripts/start_nemothrones.sh            ensure the stack is up, then open the game
#   ./scripts/start_nemothrones.sh --doctor   run `nemothrones doctor` in the sandbox instead
#
# 1. Starts the nemo stack (via ~/scripts/start_hermes.sh --no-launch if present).
# 2. Hands the sandbox the Hermes API token (for the Hermes Game Master), written
#    to the game home with mode 600. It is the sandbox's own Hermes key; nothing new
#    leaves the host.
# 3. Opens the game in the sandbox. The default entry is `nemohermes <sb> connect`
#    (a policy-enforced shell). NEMOTHRONES_ENTRY=docker uses `docker exec -it`
#    instead, which is quicker but runs outside the OpenShell supervisor's
#    process context.

set -uo pipefail
SB="${NEMOTHRONES_SANDBOX:-prometheus}"
REMOTE="${NEMOTHRONES_REMOTE:-/sandbox/.hermes/workspace/nemothrones}"
ENTRY="${NEMOTHRONES_ENTRY:-connect}"
CMD=(nemothrones play)
[[ "${1:-}" == "--doctor" ]] && CMD=(nemothrones doctor)

if [[ -x "$HOME/scripts/start_hermes.sh" ]]; then
  "$HOME/scripts/start_hermes.sh" --no-launch || { echo "stack failed to start"; exit 1; }
fi
CID="$(docker ps -q --filter "name=$SB" | head -1)"
[[ -n "$CID" ]] || { echo "sandbox '$SB' is not running"; exit 1; }

TOKEN="$(nemohermes "$SB" gateway-token --quiet 2>/dev/null || true)"
if [[ -n "$TOKEN" ]]; then
  printf '%s' "$TOKEN" | docker exec -i -u sandbox "$CID" sh -c "umask 077; cat > '$REMOTE/hermes_api_token'"
else
  echo "warning: no Hermes gateway token; the GM will fall back to inference.local"
fi

if [[ "$ENTRY" == "docker" ]]; then
  exec docker exec -it -u sandbox -e NEMOTHRONES_HOME="$REMOTE" "$CID" "$REMOTE/.venv/bin/${CMD[0]}" "${CMD[@]:1}"
fi
echo "Opening a sandbox shell. Start the game with:"
echo "    $REMOTE/.venv/bin/${CMD[*]}"
exec nemohermes "$SB" connect
