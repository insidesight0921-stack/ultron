#!/usr/bin/env bash
# AI Agent launchd 로그 회전.
# 실행 중 프로세스의 파일 디스크립터를 유지하기 위해 copytruncate 방식을 사용한다.
set -euo pipefail

PROJECT="${AI_AGENT_PROJECT:-$HOME/울트론/ai-agent}"
LOG_DIR="$PROJECT/data/logs"
MAX_BYTES="${AI_AGENT_LOG_MAX_BYTES:-10485760}"  # 10 MiB
BACKUP_COUNT="${AI_AGENT_LOG_BACKUPS:-5}"

[ -d "$LOG_DIR" ] || exit 0

rotate_one() {
    local log_file="$1"
    local size
    local i

    size=$(stat -f '%z' "$log_file" 2>/dev/null || echo 0)
    [ "$size" -ge "$MAX_BYTES" ] || return 0

    rm -f "${log_file}.${BACKUP_COUNT}.gz"
    i=$((BACKUP_COUNT - 1))
    while [ "$i" -ge 1 ]; do
        if [ -f "${log_file}.${i}.gz" ]; then
            mv "${log_file}.${i}.gz" "${log_file}.$((i + 1)).gz"
        fi
        i=$((i - 1))
    done

    cp "$log_file" "${log_file}.1"
    : > "$log_file"
    gzip -f "${log_file}.1"
    chmod 600 "$log_file" "${log_file}.1.gz"
    echo "rotated: $log_file ($size bytes)"
}

for log_file in "$LOG_DIR"/*.log; do
    [ -f "$log_file" ] || continue
    chmod 600 "$log_file"
    rotate_one "$log_file"
done
