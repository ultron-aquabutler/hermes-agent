#!/usr/bin/env bash
# Deploy the pip guard's durability mechanism on this host.
#
# Installs:
#   ~/.hermes/bin/pip_guard.py                    the canonical, bootstrap-proof copy
#   ~/.local/bin/pip -> ~/.hermes/bin/pip_guard.py the PATH entry
#   ~/.hermes/scripts/ensure_pip_guard.py         the runtime ensurer (outside the repo)
#   ~/.config/systemd/user/hermes-pip-guard.service
#   ~/.config/systemd/user/hermes-pip-guard.timer
#
# Safe to re-run: every step is idempotent.
#
# Usage: bash scripts/deploy_pip_guard.sh [--check]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HOME}/.hermes"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
UNITS="${CONFIG_HOME}/systemd/user"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

say() { printf 'pip-guard-deploy: %s\n' "$*"; }

if [[ $CHECK_ONLY -eq 1 ]]; then
    say "== check =="
    "${HERMES_HOME}/.local/bin/pip" --version 2>/dev/null || true
    python3 "${REPO}/scripts/install_pip_guard.py" --check
    systemctl --user is-enabled hermes-pip-guard.timer || true
    systemctl --user is-active  hermes-pip-guard.timer || true
    exit 0
fi

say "installing the canonical guard + symlink"
python3 "${REPO}/scripts/install_pip_guard.py"

say "installing the runtime ensurer"
install -m 0755 "${REPO}/scripts/ensure_pip_guard.py" "${HERMES_HOME}/scripts/ensure_pip_guard.py"

say "installing the systemd units"
install -m 0644 "${REPO}/scripts/systemd/hermes-pip-guard.service" "${UNITS}/hermes-pip-guard.service"
install -m 0644 "${REPO}/scripts/systemd/hermes-pip-guard.timer"  "${UNITS}/hermes-pip-guard.timer"

say "reloading systemd and arming the timer"
systemctl --user daemon-reload
systemctl --user enable --now hermes-pip-guard.timer
systemctl --user restart hermes-pip-guard.service

say "verifying"
python3 "${REPO}/scripts/install_pip_guard.py" --check
systemctl --user is-active hermes-pip-guard.timer
say "done — the guard is durable and self-healing"
