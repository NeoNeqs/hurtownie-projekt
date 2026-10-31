#!/usr/bin/env bash
#
# Prepare a fresh Ubuntu 24.04 VM for the warehouse.
#
# Run as a normal sudo-capable user, not as root:
#   bash deploy/bootstrap.sh
#
# Installs Docker, Google Chrome, rsync/tmux and uv. It does not copy the
# project, start PostgreSQL or create the Python environment; the runbook in
# deploy/azure-vm.md does that after the project is on the VM.

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as a normal user (the script uses sudo where needed)." >&2
    exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

echo "==> apt packages"
$SUDO apt-get update
$SUDO apt-get install -y \
    ca-certificates curl gnupg unzip rsync tmux \
    docker.io docker-compose-v2

echo "==> docker group"
$SUDO usermod -aG docker "$USER"

echo "==> Google Chrome"
if ! command -v google-chrome >/dev/null 2>&1; then
    $SUDO install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | $SUDO gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        | $SUDO tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
    $SUDO apt-get update
    $SUDO apt-get install -y google-chrome-stable
fi

echo "==> uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

echo
echo "Bootstrap complete."
echo "Log out and back in (or run 'newgrp docker') before using docker."
echo "Then continue with deploy/azure-vm.md, step 5."
