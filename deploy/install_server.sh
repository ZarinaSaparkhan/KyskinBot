#!/usr/bin/env bash
# Run on the new Ubuntu server after copying the reviewed application files.
# Installs the service without starting it: the Mac must stop before cutover.
set -euo pipefail
umask 077

if [[ $(id -u) -ne 0 ]]; then
    echo 'Run this installer as root on the new server.' >&2
    exit 1
fi
if [[ ! -f /etc/os-release ]]; then
    echo 'Ubuntu 24.04 is required.' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
    echo 'This installer is prepared for Ubuntu 24.04.' >&2
    exit 1
fi
if systemctl is-active --quiet kyskinbot.service; then
    echo 'A bot service is already running; use a reviewed update procedure.' >&2
    exit 1
fi
if [[ ! -f /opt/kyskinbot/bot.py || ! -f /opt/kyskinbot/deploy/requirements.txt ]]; then
    echo 'Copy the application into /opt/kyskinbot first.' >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata rsync
if ! id kyskinbot >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /opt/kyskinbot --shell /usr/sbin/nologin kyskinbot
fi
python3 -m venv /opt/kyskinbot/.venv
/opt/kyskinbot/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/kyskinbot/deploy/requirements.txt
chown -R kyskinbot:kyskinbot /opt/kyskinbot
chmod 750 /opt/kyskinbot
if [[ -f /opt/kyskinbot/.env ]]; then
    chmod 600 /opt/kyskinbot/.env
fi
install -o root -g root -m 644 /opt/kyskinbot/deploy/kyskinbot.service /etc/systemd/system/kyskinbot.service
systemctl daemon-reload
echo 'Installed. The bot has NOT been started or enabled.'
echo 'Copy the final database snapshots and .env, then run the preflight before cutover.'
