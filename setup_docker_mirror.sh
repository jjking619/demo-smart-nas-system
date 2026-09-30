#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Docker 镜像加速配置（幂等）
# 本脚本做的事：
#   1. 备份现有 /etc/docker/daemon.json
#   2. 合并写入 registry-mirrors（保留其它已有配置）
#   3. 校验 JSON 合法性（不合法则自动回滚备份）
#   4. 重启 docker 并等待就绪
#   5. 恢复重启前处于运行状态的容器
#   6. 输出校验结果
#
# 用法：
#   sudo bash setup_docker_mirror.sh
#   sudo bash setup_docker_mirror.sh --no-restart     # 仅写配置不重启
#   MIRRORS="https://a,https://b" sudo bash setup_docker_mirror.sh
# =============================================================================

DAEMON_JSON="/etc/docker/daemon.json"
RESTART=1

# 默认镜像源：均已在企业网络环境实测可用（按延迟排序）
DEFAULT_MIRRORS=(
  "https://docker.m.daocloud.io"
  "https://docker.1ms.run"
  "https://docker.xuanyuan.me"
  "https://docker.1panel.live"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-restart) RESTART=0 ;;
    -h|--help)
      sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 1
      ;;
  esac
  shift
done

log() { echo "[docker-mirror] $*"; }
warn() { echo "[docker-mirror][WARN] $*" >&2; }
die() { echo "[docker-mirror][ERROR] $*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  die "需要 root 权限运行，请使用: sudo bash $0"
fi

if command -v docker >/dev/null 2>&1; then
  :
else
  die "未检测到 docker，请先安装 docker / CasaOS"
fi

command -v python3 >/dev/null 2>&1 || die "缺少 python3，无法安全合并 JSON 配置"

# 组装镜像源列表：环境变量 MIRRORS 可覆盖（逗号分隔）
MIRROR_LIST=()
if [[ -n "${MIRRORS:-}" ]]; then
  IFS=',' read -r -a MIRROR_LIST <<< "$MIRRORS"
else
  MIRROR_LIST=("${DEFAULT_MIRRORS[@]}")
fi

log "准备写入镜像加速源（${#MIRROR_LIST[@]} 个）："
for m in "${MIRROR_LIST[@]}"; do
  echo "  - $m"
done

mkdir -p /etc/docker

BACKUP_FILE=""
if [[ -f "$DAEMON_JSON" ]]; then
  BACKUP_FILE="${DAEMON_JSON}.bak.$(date +%Y%m%d_%H%M%S)"
  cp "$DAEMON_JSON" "$BACKUP_FILE"
  log "已备份原配置: $BACKUP_FILE"
else
  log "未发现现有 daemon.json，将新建"
fi

#
# 合并写入：保留已有键，仅覆盖 registry-mirrors。
# 原文件若为非法 JSON，则中止（避免误覆盖用户配置）。
#
python3 - "$DAEMON_JSON" "${MIRROR_LIST[@]}" <<'PY'
import json
import os
import sys

path = sys.argv[1]
mirrors = [m.strip() for m in sys.argv[2:] if m.strip()]

data = {}
if os.path.exists(path) and os.path.getsize(path) > 0:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ERROR: 现有 daemon.json 不是合法 JSON，已中止以避免覆盖: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(data, dict):
            print("ERROR: 现有 daemon.json 顶层不是对象，已中止", file=sys.stderr)
            sys.exit(2)

data["registry-mirrors"] = mirrors

with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

print(f"OK: 已写入 {len(mirrors)} 个镜像源")
PY
PY_RC=$?

if [[ "$PY_RC" -ne 0 ]]; then
  if [[ -n "$BACKUP_FILE" ]]; then
    cp "$BACKUP_FILE" "$DAEMON_JSON"
    warn "已回滚为备份配置: $BACKUP_FILE"
  else
    rm -f "$DAEMON_JSON"
    warn "已删除本次新建的配置"
  fi
  die "写入镜像源失败（exit=$PY_RC）"
fi

python3 -m json.tool "$DAEMON_JSON" >/dev/null || die "JSON 校验失败，请检查 $DAEMON_JSON"
log "配置内容："
cat "$DAEMON_JSON"

if [[ "$RESTART" -ne 1 ]]; then
  warn "已按 --no-restart 跳过重启；配置将在下次 docker 重启后生效"
  warn "手动生效: systemctl restart docker"
  exit 0
fi

# ── 记录重启前运行中的容器，便于重启后核对/补起 ──
RUNNING_BEFORE="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
RUNNING_COUNT="$(printf '%s\n' "$RUNNING_BEFORE" | grep -c . || true)"
log "重启前运行中容器数: ${RUNNING_COUNT}"

log "重启 docker 使配置生效（容器会短暂中断并自动恢复）..."
systemctl restart docker

log "等待 docker 就绪..."
for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker info >/dev/null 2>&1 || die "docker 重启后未能就绪，请执行: systemctl status docker"

# ── 补起未自动恢复的容器（无 restart policy 的容器不会自动回来）──
if [[ -n "$RUNNING_BEFORE" ]]; then
  MISSING=""
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
    if [[ "$state" != "running" ]]; then
      docker start "$name" >/dev/null 2>&1 && log "已补起容器: $name" || warn "容器 $name 未能自动恢复（状态: $state）"
    fi
  done <<< "$RUNNING_BEFORE"
fi

log "当前 registry mirror 状态："
docker info 2>/dev/null | sed -n '/Registry Mirrors/,/^$/p' || true

log "完成。可用以下命令验证大镜像拉取："
echo "  sudo docker pull jellyfin/jellyfin:12.0"
