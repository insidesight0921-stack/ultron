#!/usr/bin/env bash
# ===============================================================
# AI Agent 서비스 관리 (macOS launchd)
#
# 서비스를 launchd로 등록하면 부팅/로그인 시 자동 시작.
# 크래시 나도 자동 재시작. 터미널 안 열어도 됨.
#
#   1. ai-agent.data-api  : http://127.0.0.1:8090 shareable 데이터 API
#   2. ai-agent.private-data-api : http://127.0.0.1:8091 인증 Private API
#   3. ai-agent.watch-raw : raw/ 폴더 자동 감시 → 정제 → 인덱싱
#   4. ai-agent.telegram  : 텔레그램 봇 (3단계, 폰에서 RAG 질의)
#   5. ai-agent.paper     : http://localhost:8080 paper trading 사이트 (5단계, v3.18~)
#   6. ai-agent.weekly-kium-scan : 매주 월요일 09:00 모멘텀 스캔 + 텔레그램 푸시 (v3.24~)
#   7. ai-agent.market-data-collector : 매일 16:20 공개 시장 데이터 캐시 갱신
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
umask 077

PROJECT="$HOME/울트론/ai-agent"
SCRIPTS="$PROJECT/scripts"
DATA_DIR="$PROJECT/data"
PYTHON="$PROJECT/.venv/bin/python"

if [ -x "$PYTHON" ] && [ -f "$SCRIPTS/storage_paths.py" ]; then
    LOG_DIR="$("$PYTHON" "$SCRIPTS/storage_paths.py" logs_dir)"
else
    LOG_DIR="$DATA_DIR/logs"
fi

LA_DIR="$HOME/Library/LaunchAgents"

LABEL_DATA_API="com.hyunjun.ai-agent.data-api"
LABEL_PRIVATE_DATA_API="com.hyunjun.ai-agent.private-data-api"
LABEL_WATCH="com.hyunjun.ai-agent.watch-raw"
LABEL_TG="com.hyunjun.ai-agent.telegram"
LABEL_PAPER="com.hyunjun.ai-agent.paper"
LABEL_WEEKLY="com.hyunjun.ai-agent.weekly-kium-scan"
LABEL_MARKET_COLLECTOR="com.hyunjun.ai-agent.market-data-collector"
LABEL_LOG_ROTATE="com.hyunjun.ai-agent.log-rotation"
LABEL_PRIVATE_BACKUP="com.hyunjun.ai-agent.private-backup"
LABEL_PAPER_WEEKLY="com.hyunjun.ai-agent.paper-weekly-report"
LABEL_SIGNAL_REVIEW="com.hyunjun.ai-agent.signal-review"
LABEL_PAPER_MONTHLY="com.hyunjun.ai-agent.paper-monthly-report"

PLIST_DATA_API="$LA_DIR/${LABEL_DATA_API}.plist"
PLIST_PRIVATE_DATA_API="$LA_DIR/${LABEL_PRIVATE_DATA_API}.plist"
PLIST_WATCH="$LA_DIR/${LABEL_WATCH}.plist"
PLIST_TG="$LA_DIR/${LABEL_TG}.plist"
PLIST_PAPER="$LA_DIR/${LABEL_PAPER}.plist"
PLIST_WEEKLY="$LA_DIR/${LABEL_WEEKLY}.plist"
PLIST_MARKET_COLLECTOR="$LA_DIR/${LABEL_MARKET_COLLECTOR}.plist"
PLIST_LOG_ROTATE="$LA_DIR/${LABEL_LOG_ROTATE}.plist"
PLIST_PRIVATE_BACKUP="$LA_DIR/${LABEL_PRIVATE_BACKUP}.plist"
PLIST_PAPER_WEEKLY="$LA_DIR/${LABEL_PAPER_WEEKLY}.plist"
PLIST_SIGNAL_REVIEW="$LA_DIR/${LABEL_SIGNAL_REVIEW}.plist"
PLIST_PAPER_MONTHLY="$LA_DIR/${LABEL_PAPER_MONTHLY}.plist"

LOG_DATA_API_OUT="$LOG_DIR/data_api.out.log"
LOG_DATA_API_ERR="$LOG_DIR/data_api.err.log"
LOG_PRIVATE_DATA_API_OUT="$LOG_DIR/private_data_api.out.log"
LOG_PRIVATE_DATA_API_ERR="$LOG_DIR/private_data_api.err.log"
LOG_WATCH_OUT="$LOG_DIR/watch_raw.out.log"
LOG_WATCH_ERR="$LOG_DIR/watch_raw.err.log"
LOG_TG_OUT="$LOG_DIR/telegram.out.log"
LOG_TG_ERR="$LOG_DIR/telegram.err.log"
LOG_PAPER_OUT="$LOG_DIR/paper_ui.out.log"
LOG_PAPER_ERR="$LOG_DIR/paper_ui.err.log"
LOG_WEEKLY_OUT="$LOG_DIR/weekly_kium.out.log"
LOG_WEEKLY_ERR="$LOG_DIR/weekly_kium.err.log"
LOG_MARKET_COLLECTOR_OUT="$LOG_DIR/market_data_collector.out.log"
LOG_MARKET_COLLECTOR_ERR="$LOG_DIR/market_data_collector.err.log"
LOG_PRIVATE_BACKUP_OUT="$LOG_DIR/private_backup.out.log"
LOG_PRIVATE_BACKUP_ERR="$LOG_DIR/private_backup.err.log"
LOG_PAPER_WEEKLY_OUT="$LOG_DIR/paper_weekly_report.out.log"
LOG_PAPER_WEEKLY_ERR="$LOG_DIR/paper_weekly_report.err.log"
LOG_SIGNAL_REVIEW_OUT="$LOG_DIR/signal_review.out.log"
LOG_SIGNAL_REVIEW_ERR="$LOG_DIR/signal_review.err.log"
LOG_PAPER_MONTHLY_OUT="$LOG_DIR/paper_monthly_report.out.log"
LOG_PAPER_MONTHLY_ERR="$LOG_DIR/paper_monthly_report.err.log"

mkdir -p "$LA_DIR" "$LOG_DIR"

# ─── plist 생성 ──────────────────────────────────────

write_plist_data_api() {
    cat > "$PLIST_DATA_API" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_DATA_API}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/data_api.py</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8090</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DATA_API_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DATA_API_ERR}</string>
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

write_plist_private_data_api() {
    cat > "$PLIST_PRIVATE_DATA_API" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PRIVATE_DATA_API}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/private_data_api.py</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8091</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_PRIVATE_DATA_API_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PRIVATE_DATA_API_ERR}</string>
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
    <key>Umask</key>
    <integer>63</integer>
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
    <key>Umask</key>
    <integer>63</integer>
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
        <key>AI_AGENT_DATA_API_ENABLED</key>
        <string>1</string>
        <key>AI_AGENT_PRIVATE_API_ENABLED</key>
        <string>1</string>
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
        <string>127.0.0.1</string>
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
    <key>Umask</key>
    <integer>63</integer>
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
        <key>AI_AGENT_DATA_API_ENABLED</key>
        <string>1</string>
        <key>AI_AGENT_PRIVATE_API_ENABLED</key>
        <string>1</string>
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
    <key>Umask</key>
    <integer>63</integer>
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

write_plist_market_collector() {
    cat > "$PLIST_MARKET_COLLECTOR" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_MARKET_COLLECTOR}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/market_data_collector.py</string>
        <string>--dataset</string>
        <string>all</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>20</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_MARKET_COLLECTOR_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_MARKET_COLLECTOR_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
</dict>
</plist>
EOF
}

write_plist_log_rotation() {
    cat > "$PLIST_LOG_ROTATE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_LOG_ROTATE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPTS}/rotate_logs.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
</dict>
</plist>
EOF
}

write_plist_private_backup() {
    cat > "$PLIST_PRIVATE_BACKUP" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PRIVATE_BACKUP}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/private_data_security.py</string>
        <string>all</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_PRIVATE_BACKUP_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PRIVATE_BACKUP_ERR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
</dict>
</plist>
EOF
}


# ─── .env 검증 (텔레그램 키) ─────────────────────────

write_plist_paper_weekly() {
    # StartCalendarInterval: 매주 금요일(Weekday=5) 16:30
    # 장 마감(15:30) 이후, market-data-collector(16:20) 뒤에 배치
    cat > "$PLIST_PAPER_WEEKLY" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PAPER_WEEKLY}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/paper_weekly_report.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>5</integer>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_PAPER_WEEKLY_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PAPER_WEEKLY_ERR}</string>
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

write_plist_signal_review() {
    # StartCalendarInterval: 매일 16:40 — 신호 적중률(탭 B) 결과 갱신
    # market-data-collector(16:20) 뒤, 일봉 확정 후에 돌린다.
    # 조회 실패는 다음 날 재시도된다(pending은 계속 재평가 대상).
    cat > "$PLIST_SIGNAL_REVIEW" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_SIGNAL_REVIEW}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/signal_review.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>16</integer>
        <key>Minute</key>
        <integer>40</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_SIGNAL_REVIEW_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_SIGNAL_REVIEW_ERR}</string>
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

write_plist_paper_monthly() {
    # StartCalendarInterval: 매월 1일 17:00 — **직전 달**을 집계한다(--last-month).
    # 월초에 도는 이유: 말일에 돌리면 그날 장 마감 뒤 청산분이 빠질 수 있고,
    # 달이 바뀐 뒤라야 그 달의 자산곡선이 확정된다.
    cat > "$PLIST_PAPER_MONTHLY" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PAPER_MONTHLY}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPTS}/paper_monthly_report.py</string>
        <string>--last-month</string>
        <string>--notify</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Day</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>17</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>Umask</key>
    <integer>63</integer>
    <key>WorkingDirectory</key>
    <string>${PROJECT}</string>
    <key>StandardOutPath</key>
    <string>${LOG_PAPER_MONTHLY_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PAPER_MONTHLY_ERR}</string>
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

wait_for_data_api() {
    local attempt
    for ((attempt = 1; attempt <= 50; attempt++)); do
        if /usr/bin/curl --fail --silent --max-time 1 "http://127.0.0.1:8090/health" >/dev/null 2>&1; then
            echo "  ✅ Data API 준비 완료: http://127.0.0.1:8090"
            return 0
        fi
        sleep 0.1
    done
    echo "  ⚠️  Data API 준비 확인 실패 — 소비자는 기존 로컬 DB로 fallback"
    return 1
}

wait_for_private_data_api() {
    local attempt
    for ((attempt = 1; attempt <= 50; attempt++)); do
        if /usr/bin/curl --fail --silent --max-time 1 "http://127.0.0.1:8091/health" >/dev/null 2>&1; then
            echo "  ✅ Private API 준비 완료: http://127.0.0.1:8091"
            return 0
        fi
        sleep 0.1
    done
    echo "  ❌ Private API 준비 확인 실패 — Private 소비자는 시작하지 않음"
    return 1
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
    if [ ! -f "$SCRIPTS/watch_raw.py" ]; then
        echo "❌ 스크립트 없음: watch_raw.py"
        exit 1
    fi

    if [ ! -f "$SCRIPTS/data_api.py" ]; then
        echo "❌ 스크립트 없음: data_api.py"
        exit 1
    fi

    if [ ! -f "$SCRIPTS/private_data_api.py" ]; then
        echo "❌ 스크립트 없음: private_data_api.py"
        exit 1
    fi

    if [ -f "$SCRIPTS/paper_weekly_report.py" ]; then
        write_plist_paper_weekly
        echo "  ✅ plist 생성: $PLIST_PAPER_WEEKLY"
    fi

    if [ -f "$SCRIPTS/signal_review.py" ]; then
        write_plist_signal_review
        echo "  ✅ plist 생성: $PLIST_SIGNAL_REVIEW"
    fi

    if [ -f "$SCRIPTS/paper_monthly_report.py" ]; then
        write_plist_paper_monthly
        echo "  ✅ plist 생성: $PLIST_PAPER_MONTHLY"
    fi

    write_plist_data_api
    echo "  ✅ plist 생성: $PLIST_DATA_API"

    write_plist_private_data_api
    echo "  ✅ plist 생성: $PLIST_PRIVATE_DATA_API"

    write_plist_watch
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

    if [ -f "$SCRIPTS/market_data_collector.py" ]; then
        write_plist_market_collector
        echo "  ✅ plist 생성: $PLIST_MARKET_COLLECTOR (매일 16:20)"
    fi

    write_plist_log_rotation
    echo "  ✅ plist 생성: $PLIST_LOG_ROTATE (매일 03:10)"

    write_plist_private_backup
    echo "  ✅ plist 생성: $PLIST_PRIVATE_BACKUP (매주 일 03:30)"

    cmd_start
    echo ""
    echo "🎉 설치 완료. 부팅/로그인 시 자동 시작됩니다."
    echo ""
    if [ -f "$PLIST_PAPER" ]; then
        echo "  Paper:  http://localhost:8080"
    fi
    echo "  Data API: http://127.0.0.1:8090"
    echo "  Private API: http://127.0.0.1:8091 (Bearer 인증 필요)"
    if [ "$TG_ENABLED" = "1" ]; then
        echo "  텔레그램: 봇 채팅창에서 /start 입력"
    fi
    echo "  로그:   bash $0 logs"
    echo "  상태:   bash $0 status"
}

cmd_start() {
    # 기존 로드 해제 (재시작 위해)
    [ -f "$PLIST_TG" ] && launchctl unload "$PLIST_TG" 2>/dev/null || true
    [ -f "$PLIST_PAPER" ] && launchctl unload "$PLIST_PAPER" 2>/dev/null || true
    launchctl unload "$PLIST_WATCH" 2>/dev/null || true
    [ -f "$PLIST_DATA_API" ] && launchctl unload "$PLIST_DATA_API" 2>/dev/null || true
    [ -f "$PLIST_PRIVATE_DATA_API" ] && launchctl unload "$PLIST_PRIVATE_DATA_API" 2>/dev/null || true
    [ -f "$PLIST_WEEKLY" ] && launchctl unload "$PLIST_WEEKLY" 2>/dev/null || true
    [ -f "$PLIST_MARKET_COLLECTOR" ] && launchctl unload "$PLIST_MARKET_COLLECTOR" 2>/dev/null || true
    [ -f "$PLIST_LOG_ROTATE" ] && launchctl unload "$PLIST_LOG_ROTATE" 2>/dev/null || true
    [ -f "$PLIST_PRIVATE_BACKUP" ] && launchctl unload "$PLIST_PRIVATE_BACKUP" 2>/dev/null || true
    [ -f "$PLIST_PAPER_WEEKLY" ] && launchctl unload "$PLIST_PAPER_WEEKLY" 2>/dev/null || true
    [ -f "$PLIST_SIGNAL_REVIEW" ] && launchctl unload "$PLIST_SIGNAL_REVIEW" 2>/dev/null || true
    [ -f "$PLIST_PAPER_MONTHLY" ] && launchctl unload "$PLIST_PAPER_MONTHLY" 2>/dev/null || true
    sleep 1

    # 로드
    if [ -f "$PLIST_DATA_API" ]; then
        launchctl load "$PLIST_DATA_API"
        wait_for_data_api || true
    fi
    if [ -f "$PLIST_PRIVATE_DATA_API" ]; then
        launchctl load "$PLIST_PRIVATE_DATA_API"
        wait_for_private_data_api || true
    fi
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
    if [ -f "$PLIST_MARKET_COLLECTOR" ]; then
        launchctl load "$PLIST_MARKET_COLLECTOR"
        echo "  ✅ 공개 시장 데이터 수집 스케줄 등록 (매일 16:20)"
    fi
    if [ -f "$PLIST_LOG_ROTATE" ]; then
        launchctl load "$PLIST_LOG_ROTATE"
        echo "  ✅ 로그 회전 스케줄 등록 (매일 03:10)"
    fi
    if [ -f "$PLIST_PRIVATE_BACKUP" ]; then
        launchctl load "$PLIST_PRIVATE_BACKUP"
        echo "  ✅ Private 백업 스케줄 등록 (매주 일 03:30)"
    fi
    if [ -f "$PLIST_PAPER_WEEKLY" ]; then
        launchctl load "$PLIST_PAPER_WEEKLY"
        echo "  ✅ 페이퍼 주간 성과 리포트 등록 (매주 금 16:30)"
    fi
    if [ -f "$PLIST_SIGNAL_REVIEW" ]; then
        launchctl load "$PLIST_SIGNAL_REVIEW"
        echo "  ✅ 신호 적중률 갱신 등록 (매일 16:40)"
    fi
    if [ -f "$PLIST_PAPER_MONTHLY" ]; then
        launchctl load "$PLIST_PAPER_MONTHLY"
        echo "  ✅ 페이퍼 월간 성과 리포트 등록 (매월 1일 17:00, 직전 달 집계 + 텔레그램)"
    fi
    echo "▶️  서비스 시작됨"
    sleep 2
    cmd_status
}

cmd_stop() {
    if [ -f "$PLIST_TG" ]; then
        launchctl unload "$PLIST_TG" 2>/dev/null && echo "⏸  텔레그램 봇 중지" || echo "(텔레그램 봇 이미 중지됨)"
    fi
    if [ -f "$PLIST_PAPER" ]; then
        launchctl unload "$PLIST_PAPER" 2>/dev/null && echo "⏸  paper_ui 중지" || echo "(paper_ui 이미 중지됨)"
    fi
    launchctl unload "$PLIST_WATCH" 2>/dev/null && echo "⏸  watch_raw 중지" || echo "(watch_raw 이미 중지됨)"
    if [ -f "$PLIST_DATA_API" ]; then
        launchctl unload "$PLIST_DATA_API" 2>/dev/null && echo "⏸  data-api 중지" || echo "(data-api 이미 중지됨)"
    fi
    if [ -f "$PLIST_PRIVATE_DATA_API" ]; then
        launchctl unload "$PLIST_PRIVATE_DATA_API" 2>/dev/null && echo "⏸  private-data-api 중지" || echo "(private-data-api 이미 중지됨)"
    fi
    if [ -f "$PLIST_WEEKLY" ]; then
        launchctl unload "$PLIST_WEEKLY" 2>/dev/null && echo "⏸  weekly-kium-scan 중지" || echo "(weekly-kium-scan 이미 중지됨)"
    fi
    if [ -f "$PLIST_MARKET_COLLECTOR" ]; then
        launchctl unload "$PLIST_MARKET_COLLECTOR" 2>/dev/null && echo "⏸  market-data-collector 중지" || echo "(market-data-collector 이미 중지됨)"
    fi
    if [ -f "$PLIST_LOG_ROTATE" ]; then
        launchctl unload "$PLIST_LOG_ROTATE" 2>/dev/null && echo "⏸  로그 회전 중지" || echo "(로그 회전 이미 중지됨)"
    fi
    if [ -f "$PLIST_PRIVATE_BACKUP" ]; then
        launchctl unload "$PLIST_PRIVATE_BACKUP" 2>/dev/null && echo "⏸  Private 백업 중지" || echo "(Private 백업 이미 중지됨)"
    fi
    if [ -f "$PLIST_PAPER_WEEKLY" ]; then
        launchctl unload "$PLIST_PAPER_WEEKLY" 2>/dev/null && echo "⏸  페이퍼 주간 리포트 중지" || echo "(페이퍼 주간 리포트 이미 중지됨)"
    fi
    if [ -f "$PLIST_SIGNAL_REVIEW" ]; then
        launchctl unload "$PLIST_SIGNAL_REVIEW" 2>/dev/null && echo "⏸  신호 적중률 갱신 중지" || echo "(신호 적중률 갱신 이미 중지됨)"
    fi
    if [ -f "$PLIST_PAPER_MONTHLY" ]; then
        launchctl unload "$PLIST_PAPER_MONTHLY" 2>/dev/null && echo "⏸  페이퍼 월간 리포트 중지" || echo "(페이퍼 월간 리포트 이미 중지됨)"
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

    LABELS=()
    [ -f "$PLIST_PAPER_WEEKLY" ] && LABELS+=("$LABEL_PAPER_WEEKLY")
    [ -f "$PLIST_DATA_API" ] && LABELS+=("$LABEL_DATA_API")
    [ -f "$PLIST_PRIVATE_DATA_API" ] && LABELS+=("$LABEL_PRIVATE_DATA_API")
    LABELS+=("$LABEL_WATCH")
    [ -f "$PLIST_TG" ] && LABELS+=("$LABEL_TG")
    [ -f "$PLIST_PAPER" ] && LABELS+=("$LABEL_PAPER")
    [ -f "$PLIST_WEEKLY" ] && LABELS+=("$LABEL_WEEKLY")
    [ -f "$PLIST_MARKET_COLLECTOR" ] && LABELS+=("$LABEL_MARKET_COLLECTOR")
    [ -f "$PLIST_LOG_ROTATE" ] && LABELS+=("$LABEL_LOG_ROTATE")
    [ -f "$PLIST_PRIVATE_BACKUP" ] && LABELS+=("$LABEL_PRIVATE_BACKUP")
    [ -f "$PLIST_SIGNAL_REVIEW" ] && LABELS+=("$LABEL_SIGNAL_REVIEW")
    [ -f "$PLIST_PAPER_MONTHLY" ] && LABELS+=("$LABEL_PAPER_MONTHLY")

    for label in "${LABELS[@]}"; do
        info=$(launchctl list | grep "$label" || echo "")
        if [ -n "$info" ]; then
            pid=$(echo "$info" | awk '{print $1}')
            status=$(echo "$info" | awk '{print $2}')
            if [ "$pid" = "-" ]; then
                if [ "$status" = "0" ]; then
                    printf "  🕒 %-40s 로드됨 (다음 실행 대기)\n" "$label"
                else
                    printf "  ❌ %-40s 중지됨 (마지막 종료 코드: %s)\n" "$label" "$status"
                fi
            else
                printf "  ✅ %-40s PID %s 실행 중\n" "$label" "$pid"
            fi
        else
            printf "  ⚪ %-40s 미설치\n" "$label"
        fi
    done

    echo ""
    if [ -f "$PLIST_PAPER" ]; then
        echo "📍 Paper:  http://localhost:8080"
    fi
    if [ -f "$PLIST_DATA_API" ]; then
        echo "📍 Data API: http://127.0.0.1:8090"
    fi
    if [ -f "$PLIST_PRIVATE_DATA_API" ]; then
        echo "📍 Private API: http://127.0.0.1:8091 (Bearer 인증 필요)"
    fi
    if command -v tailscale >/dev/null 2>&1; then
        TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
        if [ -n "$TS_IP" ]; then
            echo "📍 Tailscale: http://${TS_IP}:8080"
        fi
    fi
    echo "📍 로그:   $LOG_DIR/"
}

cmd_logs() {
    echo "📜 마지막 30줄 (Ctrl+C로 종료)"
    if [ -f "$LOG_DATA_API_OUT" ] || [ -f "$LOG_DATA_API_ERR" ]; then
        echo ""
        echo "── data_api.out ──"
        tail -n 30 "$LOG_DATA_API_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── data_api.err ──"
        tail -n 30 "$LOG_DATA_API_ERR" 2>/dev/null || echo "(없음)"
    fi
    if [ -f "$LOG_PRIVATE_DATA_API_OUT" ] || [ -f "$LOG_PRIVATE_DATA_API_ERR" ]; then
        echo ""
        echo "── private_data_api.out ──"
        tail -n 30 "$LOG_PRIVATE_DATA_API_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── private_data_api.err ──"
        tail -n 30 "$LOG_PRIVATE_DATA_API_ERR" 2>/dev/null || echo "(없음)"
    fi
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
    if [ -f "$LOG_MARKET_COLLECTOR_OUT" ] || [ -f "$LOG_MARKET_COLLECTOR_ERR" ]; then
        echo ""
        echo "── market_data_collector.out ──"
        tail -n 30 "$LOG_MARKET_COLLECTOR_OUT" 2>/dev/null || echo "(없음)"
        echo ""
        echo "── market_data_collector.err ──"
        tail -n 30 "$LOG_MARKET_COLLECTOR_ERR" 2>/dev/null || echo "(없음)"
    fi
}

cmd_logs_follow() {
    echo "📜 실시간 로그 (Ctrl+C로 종료)"
    FILES=()
    [ -f "$LOG_DATA_API_OUT" ] && FILES+=("$LOG_DATA_API_OUT")
    [ -f "$LOG_DATA_API_ERR" ] && FILES+=("$LOG_DATA_API_ERR")
    [ -f "$LOG_PRIVATE_DATA_API_OUT" ] && FILES+=("$LOG_PRIVATE_DATA_API_OUT")
    [ -f "$LOG_PRIVATE_DATA_API_ERR" ] && FILES+=("$LOG_PRIVATE_DATA_API_ERR")
    FILES+=("$LOG_WATCH_OUT" "$LOG_WATCH_ERR")
    [ -f "$LOG_TG_OUT" ] && FILES+=("$LOG_TG_OUT")
    [ -f "$LOG_TG_ERR" ] && FILES+=("$LOG_TG_ERR")
    [ -f "$LOG_PAPER_OUT" ] && FILES+=("$LOG_PAPER_OUT")
    [ -f "$LOG_PAPER_ERR" ] && FILES+=("$LOG_PAPER_ERR")
    [ -f "$LOG_WEEKLY_OUT" ] && FILES+=("$LOG_WEEKLY_OUT")
    [ -f "$LOG_WEEKLY_ERR" ] && FILES+=("$LOG_WEEKLY_ERR")
    [ -f "$LOG_MARKET_COLLECTOR_OUT" ] && FILES+=("$LOG_MARKET_COLLECTOR_OUT")
    [ -f "$LOG_MARKET_COLLECTOR_ERR" ] && FILES+=("$LOG_MARKET_COLLECTOR_ERR")
    tail -F "${FILES[@]}" 2>/dev/null
}

cmd_install_log_rotation() {
    if [ ! -x "$SCRIPTS/rotate_logs.sh" ]; then
        echo "❌ 실행 파일 없음: $SCRIPTS/rotate_logs.sh"
        exit 1
    fi
    write_plist_log_rotation
    launchctl unload "$PLIST_LOG_ROTATE" 2>/dev/null || true
    launchctl load "$PLIST_LOG_ROTATE"
    echo "✅ 로그 회전 설치: 매일 03:10, 10MiB, 압축 백업 5개"
}

cmd_install_private_backup() {
    if [ ! -f "$SCRIPTS/private_data_security.py" ]; then
        echo "❌ 실행 파일 없음: $SCRIPTS/private_data_security.py"
        exit 1
    fi
    write_plist_private_backup
    launchctl unload "$PLIST_PRIVATE_BACKUP" 2>/dev/null || true
    launchctl load "$PLIST_PRIVATE_BACKUP"
    echo "✅ Private 백업 설치: 매주 일요일 03:30, 자동 삭제 없음"
}

cmd_install_private_data_api() {
    if [ ! -x "$PYTHON" ] || [ ! -f "$SCRIPTS/private_data_api.py" ]; then
        echo "❌ Private API 실행 환경을 찾을 수 없습니다."
        exit 1
    fi
    write_plist_private_data_api
    launchctl unload "$PLIST_PRIVATE_DATA_API" 2>/dev/null || true
    launchctl load "$PLIST_PRIVATE_DATA_API"
    if ! wait_for_private_data_api; then
        echo "   로그 확인: $LOG_PRIVATE_DATA_API_ERR"
        exit 1
    fi
    echo "✅ Private API 설치: http://127.0.0.1:8091 (Bearer 인증 필요)"
}

cmd_rotate_logs() {
    bash "$SCRIPTS/rotate_logs.sh"
}

cmd_restart_paper() {
    if [ ! -x "$PYTHON" ] || [ ! -f "$SCRIPTS/paper_ui.py" ]; then
        echo "❌ Paper UI 실행 환경을 찾을 수 없습니다."
        exit 1
    fi
    write_plist_paper
    launchctl unload "$PLIST_PAPER" 2>/dev/null || true
    launchctl load "$PLIST_PAPER"
    echo "✅ Paper UI 재시작: http://127.0.0.1:8080"
}

cmd_restart_telegram() {
    if [ ! -x "$PYTHON" ] || [ ! -f "$SCRIPTS/telegram_bot.py" ]; then
        echo "❌ Telegram 실행 환경을 찾을 수 없습니다."
        exit 1
    fi
    if ! check_telegram_env; then
        exit 1
    fi
    write_plist_telegram
    launchctl unload "$PLIST_TG" 2>/dev/null || true
    launchctl load "$PLIST_TG"
    echo "✅ Telegram 재시작: Private watchlist·일정 읽기 API 우선"
}

cmd_uninstall() {
    echo "🗑  AI Agent 서비스 제거 중..."
    cmd_stop
    launchctl unload "$PLIST_LOG_ROTATE" 2>/dev/null || true
    launchctl unload "$PLIST_PRIVATE_BACKUP" 2>/dev/null || true
    rm -f "$PLIST_DATA_API" "$PLIST_PRIVATE_DATA_API" "$PLIST_WATCH" "$PLIST_TG" "$PLIST_PAPER" "$PLIST_WEEKLY" "$PLIST_MARKET_COLLECTOR" "$PLIST_LOG_ROTATE" "$PLIST_PRIVATE_BACKUP"
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
  bash $0 restart-paper  Paper UI 설정 갱신 + 단독 재시작
  bash $0 restart-telegram  Telegram 설정 갱신 + 단독 재시작
  bash $0 logs        최근 로그 보기
  bash $0 follow      실시간 로그 (Ctrl+C로 종료)
  bash $0 install-log-rotation  로그 회전만 설치 (매일 03:10)
  bash $0 install-private-backup  Private DB 주간 백업만 설치
  bash $0 install-private-data-api  Private API만 설치·시작
  bash $0 rotate-logs 현재 10MiB 이상 로그 즉시 회전
  bash $0 uninstall   서비스 제거 (데이터는 보존)

설치 후:
  - 부팅/로그인 시 자동 시작
  - 크래시 나면 자동 재시작
  - Data API: http://127.0.0.1:8090 (로컬 전용, shareable 데이터만)
  - Private API: http://127.0.0.1:8091 (로컬 전용, Bearer 인증)
  - 공개 시장 데이터 수집: 매일 16:20 지수·ticker·factor·universe 캐시 갱신
  - Paper:  http://localhost:8080  (5단계 검증 사이트, v3.18~)
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
    restart-paper) cmd_restart_paper ;;
    restart-telegram) cmd_restart_telegram ;;
    status)     cmd_status ;;
    logs)       cmd_logs ;;
    follow)     cmd_logs_follow ;;
    install-log-rotation) cmd_install_log_rotation ;;
    install-private-backup) cmd_install_private_backup ;;
    install-private-data-api) cmd_install_private_data_api ;;
    rotate-logs) cmd_rotate_logs ;;
    uninstall)  cmd_uninstall ;;
    *)          cmd_help ;;
esac
