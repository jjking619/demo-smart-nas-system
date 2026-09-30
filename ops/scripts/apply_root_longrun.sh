#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "[1/6] install voice-bridge service (with MemoryHigh/MemoryMax)"
bash "$APP_DIR/local_voice_chat/install_voice_bridge_service.sh"

echo "[2/6] install system backup/prune units"
install -m 0644 "$APP_DIR/ops/systemd/immich-pg-backup.service" /etc/systemd/system/immich-pg-backup.service
install -m 0644 "$APP_DIR/ops/systemd/immich-pg-backup.timer" /etc/systemd/system/immich-pg-backup.timer
install -m 0644 "$APP_DIR/ops/systemd/docker-prune-weekly.service" /etc/systemd/system/docker-prune-weekly.service
install -m 0644 "$APP_DIR/ops/systemd/docker-prune-weekly.timer" /etc/systemd/system/docker-prune-weekly.timer

echo "[3/6] install journald limit"
mkdir -p /etc/systemd/journald.conf.d
install -m 0644 "$APP_DIR/ops/systemd/90-ai-nas-journald.conf" /etc/systemd/journald.conf.d/90-ai-nas.conf

echo "[4/6] reload systemd + enable timers"
systemctl daemon-reload
systemctl enable --now immich-pg-backup.timer docker-prune-weekly.timer

echo "[5/6] restart journald"
systemctl restart systemd-journald

echo "[6/6] show status"
systemctl show voice-bridge -p MemoryHigh -p MemoryMax -p Restart -p RestartSec --no-pager
systemctl list-timers --all --no-pager | grep -E 'immich-pg-backup|docker-prune-weekly' || true
journalctl --disk-usage

echo "done"
