#!/usr/bin/env bash
# deploy.sh - install NemoThrones into the nemo stack's OpenShell sandbox.
#
#   ./scripts/deploy.sh               build, copy, install, enable plugin, apply policy
#   ./scripts/deploy.sh --no-policy   skip the egress preset (if already applied)
#   ./scripts/deploy.sh --check       report only; change nothing
#
# Everything lands under /sandbox/.hermes (the only persistent path), so a sandbox
# rebuild keeps it and `hermes --backup` copies it. Packages are installed OFFLINE
# from a host-built wheelhouse, so the sandbox needs no PyPI egress to deploy.
# Copies use `docker cp` + `chown sandbox:sandbox`, the path the nemo README verifies
# for workspace restores.

set -uo pipefail
SB="${NEMOTHRONES_SANDBOX:-prometheus}"
REMOTE="${NEMOTHRONES_REMOTE:-/sandbox/.hermes/workspace/nemothrones}"
HERMES_HOME=/sandbox/.hermes
REPO="$(cd "$(dirname "$0")/.." && pwd)"
POLICY=1; CHECK=0
for a in "$@"; do
  case "$a" in
    --no-policy) POLICY=0 ;;
    --check) CHECK=1 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; exit 1; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "Preflight"
for t in nemohermes docker python3; do command -v "$t" >/dev/null 2>&1 || bad "$t not found"; done
CID="$(docker ps -q --filter "name=$SB" | head -1)"
[[ -n "$CID" ]] || bad "sandbox '$SB' is not running: start it with \`hermes --no-launch\`"
ok "sandbox $SB running (container ${CID:0:12})"
sx() { docker exec -u sandbox "$CID" "$@"; }
PYV="$(sx python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)" \
  || bad "python3 not found inside the sandbox"
ok "sandbox python $PYV"
(( CHECK )) && { ok "check only: nothing changed"; exit 0; }

step "Build on the host"
cd "$REPO"
rm -rf dist wheelhouse && mkdir -p dist wheelhouse
python3 -m pip wheel --no-deps -q -w dist . || bad "wheel build failed"
WHEEL="$(ls dist/nemothrones-*.whl | head -1)"
ok "built $(basename "$WHEEL")"
python3 -m pip download -q --only-binary=:all: --python-version "$PYV" \
  --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform any \
  -d wheelhouse "$WHEEL" || bad "could not download dependency wheels for python $PYV"
ok "wheelhouse: $(ls wheelhouse | wc -l) wheels for python $PYV"

step "Copy into the sandbox"
docker exec "$CID" mkdir -p "$REMOTE" "$HERMES_HOME/plugins" "$HERMES_HOME/skills"
docker exec "$CID" rm -rf "$REMOTE/dist" "$REMOTE/wheelhouse"
docker cp dist "$CID:$REMOTE/dist"
docker cp wheelhouse "$CID:$REMOTE/wheelhouse"
docker exec "$CID" rm -rf "$HERMES_HOME/plugins/nemothrones"
docker cp hermes/plugin/nemothrones "$CID:$HERMES_HOME/plugins/nemothrones"
for s in hermes/skills/*/; do
  name="$(basename "$s")"
  docker exec "$CID" rm -rf "$HERMES_HOME/skills/$name"
  docker cp "$s" "$CID:$HERMES_HOME/skills/$name"
done
if ! sx test -f "$REMOTE/nemothrones.yaml"; then
  docker cp nemothrones.example.yaml "$CID:$REMOTE/nemothrones.yaml"
fi
docker exec "$CID" chown -R sandbox:sandbox "$REMOTE" "$HERMES_HOME/plugins/nemothrones" "$HERMES_HOME/skills"
ok "files in place"

step "Install the game venv (offline)"
sx sh -c "test -x '$REMOTE/.venv/bin/python' || python3 -m venv '$REMOTE/.venv'" || bad "venv creation failed"
sx "$REMOTE/.venv/bin/pip" install -q --no-index --find-links "$REMOTE/wheelhouse" --upgrade \
  "$REMOTE/dist/$(basename "$WHEEL")" || bad "pip install failed"
sx "$REMOTE/.venv/bin/nemothrones" --version && ok "installed"

step "Enable the Hermes plugin and AGENTS.md section"
sx /opt/hermes/.venv/bin/python - <<'PY' && ok "plugin 'nemothrones' enabled in config.yaml" || warn "could not edit config.yaml: add 'nemothrones' to plugins.enabled by hand"
import pathlib, shutil, yaml
p = pathlib.Path("/sandbox/.hermes/config.yaml")
cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
cfg = cfg or {}
plugins = cfg.setdefault("plugins", {}) or {}
cfg["plugins"] = plugins
enabled = plugins.setdefault("enabled", []) or []
if "nemothrones" not in enabled:
    enabled.append("nemothrones")
    plugins["enabled"] = enabled
    if p.exists():
        shutil.copy2(p, p.with_suffix(".yaml.bak-nemothrones"))
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
if sx sh -c "grep -q 'nemothrones:begin' '$HERMES_HOME/workspace/AGENTS.md' 2>/dev/null"; then
  ok "AGENTS.md already has the NemoThrones section"
else
  docker exec -i -u sandbox "$CID" sh -c "cat >> '$HERMES_HOME/workspace/AGENTS.md'" < hermes/AGENTS.nemothrones.md \
    && ok "appended NemoThrones section to AGENTS.md"
fi

if (( POLICY )); then
  step "Egress preset"
  nemohermes "$SB" policy-add --from-file policy/nemothrones.yaml --yes \
    && ok "policy 'nemothrones' applied" \
    || warn "policy-add failed; apply by hand: nemohermes $SB policy-add --from-file policy/nemothrones.yaml"
fi

step "Done"
echo "Restart Hermes so it loads the plugin:   hermes --stop && hermes --no-launch"
echo "Then play:                               ./scripts/start_nemothrones.sh"
echo "Check the stack from inside the sandbox: $REMOTE/.venv/bin/nemothrones doctor"
