#!/usr/bin/env bash
# ===============================================================
# AI Agent 서비스 관리 (macOS launchd)
#
# 세 서비스를 launchd로 등록하면 부팅/로그인 시 자동 시작.
# 크래시 나도 자동 재시작. 터미널 안 열어도 됨.
#
#   1. ai-agent.web-ui    : http://localhost:8080 (+ Tailscale IP) 채팅 UI
#   2. ai-agent.watch-raw : raw/ 폴더 자동 감시 → 정제 → 인덱싱
#   3. ai-agent.telegram  : 텔레그램 봇 (3단계, 폰에서 RAG 질의)
#   4. ai-agent.paper     : http://localhost:8081 paper trading 사이트 (5단계, v3.18~)
#   5. ai-agent.weekly-kium-scan : 매주 월요일 09:00 모멘텀 스캔 + 텔레그램 푸시 (v3.24~)
#
# 사용:
#   bash agent_services.sh install     # 최초 설치 + 시작
#   bash agent_services.sh status      # 동작 상태 확인
#   bash agent_services.sh stop        # 일시 중지
#   bash agent_services.sh start       # 다시 시작
#   bash agent_services.sh restart     # 재시작
#   bash agent_services.sh logs        # 로그 tail
#   bash agent_services.sh uninstall   # 완전 제거
# ===============================================================
set -euo pipefail

PROJECT="$HOME/울트론/ai-agent"
SCRIPTS="$PROJECT/scripts"
DATA_DIR="$PROJECT/data"
PYTHON="$PROJECT/.venv/bin/python"

LA_DIR="$HOME/Library/LaunchAgents"

LABEL_WEB="com.hyunjun.ai-agent.web-ui"
LABEL_WATCH="com.hyunjun.ai-agent.watch-raw"
LABEL_TG="com.hyunjun.ai-agent.telegram"
LABEL_PAPER="com.hyunjun.ai-agent.paper"
LABEL_WEEKLY="com.hyunjun.ai-agent.weekly-kium-scan"

PLIST_WEB="$LA_DIR/${LABEL_WEB}.plist"
PLIST_WATCH="$LA_DIR/${LABEL_WATCH}.plist"
PLIST_TG="$LA_DIR/${LABEL_TG}.plist"
PLIST_PAPER="$LA_DIR/${LABEL_PAPER}.plist"
PLIST_WEEKLY="$LA_DIR/${LABEL_WEEKLY}.plist"

LOG_WEB_OUT="$DATA_DIR/logs/web_ui.out.log"
LOG_WEB_ERR="$DATA_DIR/logs/web_ui.err.log"
LOG_WATCH_OUT="$DATA_DIR/logs/watch_raw.out.log"
LOG_WATCH_ERR="$DATA_DIR/logs/watch_raw.err.log"
LOG_TG_OUT="$DATA_DIR/logs/telegram.out.log"
LOG_TG_ERR="$DATA_DIR/logs/telegram.err.log"
LOG_PAPER_OUT="$DATA_DIR/logs/paper_ui.out.log"
LOG_PAPER_ERR="$DATA_DIR/logs/paper_ui.err.log"
LOG_WEEKLY_OUT="$DATA_DIR/logs/weekly_kium.out.log"
LOG_WEEKLY_ERR="$DATA_DIR/logs/weekly_kium.err.log"

mkdir -p "$LA_DIR" "$DATA_DIR/logs"

# ─── plist 생성 ──────────────────────────────────────

write_plist_web() {
    cat > "$PLIST_WEB" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_WEB}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/web_ui.py</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8080</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_WEB_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_WEB_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF
}

write_plist_watch() {
    cat > "$PLIST_WATCH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_WATCH}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/watch_raw.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_WATCH_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_WATCH_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF
}

write_plist_telegram() {
    cat > "$PLIST_TG" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_TG}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/telegram_bot.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_TG_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_TG_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF
}

write_plist_paper() {
    cat > "$PLIST_PAPER" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PAPER}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/paper_ui.py</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8081</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_PAPER_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PAPER_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF
}

write_plist_weekly() {
    # StartCalendarInterval: 매주 월요일(Weekday=1) 09:00
    cat > "$PLIST_WEEKLY" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_WEEKLY}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/weekly_kium_scan.py</string>
        <string>--top</string>
        <string>20</string>
        <string>--market</string>
        <string>KOSPI200</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_WEEKLY_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_WEEKLY_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOF
}


# ─── .env 검증 (텔레그램 키) ─────────────────────────

check_telegram_env() {
    if [ ! -f "$PROJECT/.env" ]; then
        echo "⚠️  .env 없음 — 텔레그램 봇 비활성화"
        return 1
    fi
    # shell이 .env를 직접 읽음 (export 안 된 단순 KEY=value 형식)
    local token
    local allowed
    token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$PROJECT/.env" | head -1 | cut -d'=' -f2-)
    allowed=$(grep -E '^ALLOWED_TELEGRAM_USER_ID=' "$PROJECT/.env" | head -1 | cut -d'=' -f2-)
    if [ -z "$token" ] || [ -z "$allowed" ]; then
        echo "⚠️  .env에 TELEGRAM_BOT_TOKEN 또는 ALLOWED_TELEGRAM_USER_ID 없음 — 텔레그램 봇 비활성화"
        return 1
    fi
    return 0
}


# ─── 명령 디스패치 ───────────────────────────────────

cmd_install() {
    echo "🔧 AI Agent 서비스 설치 중..."

    # Python venv 검증
    if [ ! -x "$PYTHON" ]; then
        echo "❌ Python venv 없음: $PYTHON"
        echo "   먼저: bash $SCRIPTS/setup_python.sh"
        exit 1
    fi

    # 스크립트 존재 검증
    if [ ! -f "$SCRIPTS/web_ui.py" ] || [ ! -f "$SCRIPTS/watch_raw.py" ]; then
        echo "❌ 스크립트 없음. web_ui.py / watch_raw.py 확인 필요"
        exit 1
    fi

    write_plist_web
    write_plist_watch
    echo "  ✅ plist 생성: $PLIST_WEB"
    echo "  ✅ plist 생성: $PLIST_WATCH"

    # paper trading (5단계 v3.18+) — 항상 등록 (외부 키 무관)
    if [ -f "$SCRIPTS/paper_ui.py" ]; then
        write_plist_paper
        echo "  ✅ plist 생성: $PLIST_PAPER"
    fi

    # 텔레그램 — .env에 키 있을 때만 등록
    TG_ENABLED=0
    if [ -f "$SCRIPTS/telegram_bot.py" ] && check_telegram_env; then
        write_plist_telegram
        echo "  ✅ plist 생성: $PLIST_TG"
        TG_ENABLED=1
    fi

    # 주간 자동 스캔 (v3.24) — weekly_kium_scan.py 있으면 항상 등록
    if [ -f "$SCRIPTS/weekly_kium_scan.py" ]; then
        write_plist_weekly
        echo "  ✅ plist 생성: $PLIST_WEEKLY (매주 월요일 09:00)"
    fi

    cmd_start
    echo ""
    echo "🎉 설치 완료. 부팅/로그인 시 자동 시작됩니다."
    echo ""
    echo "  웹 UI:  http://localhost:8080"
    if [ -f "$PLIST_PAPER" ]; then
        echo "  Paper:  http://localhost:8081"
    fi
    if [ "$TG_ENABLED" = "1" ]; then
        echo "  텔레그램: 봇 채팅창에서 /start 입력"
    fi
    echo "  로그:   bash $0 logs"
    echo "  상태:   bash $0 status"
}

cmd_start() {
    # 기존 로드 해제 (재시작 위해)
    launchctl unload "$PLIST_WEB" 2>/dev/null || true
    launchctl unload "$PLIST_WATCH" 2>/dev/null || true
    [ -f "$PLIST_TG" ] && launchctl unload "$PLIST_TG" 2>/dev/null || true
    [ -f "$PLIST_PAPER" ] && launchctl unload "$PLIST_PAPER" 2>/dev/null || true
    [ -f "$PLIST_WEEKLY" ] && launchctl unload "$PLIST_WEEKLY" 2>/dev/null || true
    sleep 1

    # 로드
    launchctl load "$PLIST_WEB"
    launchctl load "$PLIST_WATCH"
    if [ -f "$PLIST_TG" ]; then
        launchctl load "$PLIST_TG"
    fi
    if [ -f "$PLIST_PAPER" ]; then
        launchctl load "$PLIST_PAPER"
    fi
    if [ -f "$PLIST_WEEKLY" ]; then
        launchctl load "$PLIST_WEEKLY"
        echo "  ✅ 주간 스캔 스케줄 등록 (매주 월 09:00)"
    fi
    echo "▶️  서비스 시작됨"
    sleep 2
    cmd_status
}

cmd_stop() {
    launchctl unload "$PLIST_WEB" 2>/dev/null && echo "⏸  웹 UI 중지" || echo "(웹 UI 이미 중지됨)"
    launchctl unload "$PLIST_WATCH" 2>/dev/null && echo "⏸  watch_raw 중지" || echo "(watch_raw 이미 중지됨)"
    if [ -f "$PLIST_TG" ]; then
        launchctl unload "$PLIST_TG" 2>/dev/null && echo "⏸  텔레그램 봇 중지" || echo "(텔레그램 봇 이미 중지됨)"
    fi
    if [ -f "$PLIST_PAPER" ]; then
        launchctl unload "$PLIST_PAPER" 2>/dev/null && echo "⏸  paper_ui 중지" || echo "(paper_ui 이미 중지됨)"
    fi
    if [ -f "$PLIST_WEEKLY" ]; then
        launchctl unload "$PLIST_WEEKLY" 2>/dev/null && echo "⏸  weekly-kium-scan 중지" || echo "(weekly-kium-scan 이미 중지됨)"
    fi
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    echo ""
    echo "📊 AI Agent 서비스 상태"
    echo "============================================"

    LABELS=("$LABEL_WEB" "$LABEL_WATCH")
    [ -f "$PLIST_TG" ] && LABELS+=("$LABEL_TG")
    [ -f "$PLIST_PAPER" ] && LABELS+=("$LABEL_PAPER")
    [ -f "$PLIST_WEEKLY" ] && LABELS+=("$LABEL_WEEKLY")

    for label in "${LABELS[@]}"; do
        info=$(launchctl list | grep "$label" || echo "")
        if [ -n "$info" ]; then
            pid=$(echo "$info" | awk '{print $1}')
            status=$(echo "$info" | awk '{print $2}')
            if [ "$pid" = "-" ]; then
                printf "  ❌ %-40s 중지됨 (마지막 종료 코드: %s)\n" "$label" "$status"
            else
                printf "  ✅ %-40s PID %s 실행 중\n" "$label" "$pid"
            fi
        else
            printf "  ⚪ %-40s 미설치\n" "$label"
        fi
    done

    echo ""
    echo "📍 웹 UI:  http://localhost:8080"
    if [ -f "$PLIST_PAPER" ]; then
        echo "📍 Paper:  http://localhost:8081"
    fi
    if command -v tailscale >/dev/null 2>&1; then
        TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
        if [ -n "$TS_IP" ]; then
            echo "📍 Tailscale: http://${TS_IP}:8080  ·  http://${TS_IP}:8081"
        fi
    fi
    echo "📍 로그:   $DATA_DIR/logs/"
}

cmd_logs() {
    echo "📜 마지막 30줄 (Ctrl+C로 종료)"
    echo ""
    echo "── web_ui.out ──"
    tail -n 30 "$LOG_WEB_OUT" 2>/dev/null || echo "(없음)"
    echo ""
    echo "── web_ui.err ──"
    tail -n 30 "$LOG_WEB_ERR" 2>/dev/null || echo "(없음)"
    echo ""
    echo "── watch_raw.out ──"
    tail -n 30 "$LOG_WATCH_OUT" 2>/dev/null || echo "(없음)"
    echo ""
    echo "── watch_raw.err ──"
    tail -n 30 "$LOG_WATCH_ERR" 2>/dev/null || echo "(없음)"
    if [ -f "$LOG_TG_OUT" ] || [ -f "$LOG_TG_ERR" ]; then
        echo ""
        echo "── telegram.out ──"
        tail -n 30 "$LOG_TG_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── telegram.err ──"
        tail -n 30 "$LOG_TG_ERR" 2>/dev/null || echo "(없음)"
    fi
    if [ -f "$LOG_PAPER_OUT" ] || [ -f "$LOG_PAPER_ERR" ]; then
        echo ""
        echo "── paper_ui.out ──"
        tail -n 30 "$LOG_PAPER_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── paper_ui.err ──"
        tail -n 30 "$LOG_PAPER_ERR" 2>/dev/null || echo "(없음)"
    fi
    if [ -f "$LOG_WEEKLY_OUT" ] || [ -f "$LOG_WEEKLY_ERR" ]; then
        echo ""
        echo "── weekly_kium.out ──"
        tail -n 50 "$LOG_WEEKLY_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── weekly_kium.err ──"
        tail -n 50 "$LOG_WEEKLY_ERR" 2>/dev/null || echo "(없음)"
    fi
}

cmd_logs_follow() {
    echo "📜 실시간 로그 (Ctrl+C로 종료)"
    FILES=("$LOG_WEB_OUT" "$LOG_WEB_ERR" "$LOG_WATCH_OUT" "$LOG_WATCH_ERR")
    [ -f "$LOG_TG_OUT" ] && FILES+=("$LOG_TG_OUT")
    [ -f "$LOG_TG_ERR" ] && FILES+=("$LOG_TG_ERR")
    [ -f "$LOG_PAPER_OUT" ] && FILES+=("$LOG_PAPER_OUT")
    [ -f "$LOG_PAPER_ERR" ] && FILES+=("$LOG_PAPER_ERR")
    [ -f "$LOG_WEEKLY_OUT" ] && FILES+=("$LOG_WEEKLY_OUT")
    [ -f "$LOG_WEEKLY_ERR" ] && FILES+=("$LOG_WEEKLY_ERR")
    tail -F "${FILES[@]}" 2>/dev/null
}

cmd_uninstall() {
    echo "🗑  AI Agent 서비스 제거 중..."
    cmd_stop
    rm -f "$PLIST_WEB" "$PLIST_WATCH" "$PLIST_TG" "$PLIST_PAPER" "$PLIST_WEEKLY"
    echo "  ✅ plist 파일 삭제"
    echo ""
    echo "완전히 제거되었습니다. 데이터(wiki, LanceDB, 로그)는 그대로 보존됩니다."
}

cmd_help() {
    cat <<EOF
AI Agent 서비스 관리

사용:
  bash $0 install     최초 설치 + 시작 (한 번만)
  bash $0 status      현재 상태 확인
  bash $0 start       시작
  bash $0 stop        중지
  bash $0 restart     재시작
  bash $0 logs        최근 로그 보기
  bash $0 follow      실시간 로그 (Ctrl+C로 종료)
  bash $0 uninstall   서비스 제거 (데이터는 보존)

설치 후:
  - 부팅/로그인 시 자동 시작
  - 크래시 나면 자동 재시작
  - 웹 UI:  http://localhost:8080  (+ Tailscale IP:8080)
  - Paper:  http://localhost:8081  (5단계 검증 사이트, v3.18~)
  - 텔레그램: 봇 채팅창 (.env에 토큰 + user_id 설정 시)
  - 터미널 안 열어도 됨
EOF
}

# ─── main ────────────────────────────────────────────

case "${1:-help}" in
    install)    cmd_install ;;
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    status)     cmd_status ;;
    logs)       cmd_logs ;;
    follow)     cmd_logs_follow ;;
    uninstall)  cmd_uninstall ;;
    *)          cmd_help ;;
esac
