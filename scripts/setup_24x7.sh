#!/usr/bin/env bash
# ===============================================================
# setup_24x7.sh — AI Agent 24/7 운영 환경 설정
#
# 목적:
#   1. pmset 절전 완전 비활성화 (어댑터 연결 상태 기준)
#   2. 클램셸 모드 지원 확인
#   3. 매주 월요일 08:50 자동 wake 등록 (주간 스캔 09:00 전 준비)
#   4. 현재 설정 상태 점검 (check 명령)
#   5. 원복 방법 안내 (reset 명령)
#
# 사용:
#   bash setup_24x7.sh apply    # 설정 적용 (sudo 필요)
#   bash setup_24x7.sh check    # 현재 상태 확인
#   bash setup_24x7.sh wake     # wake 스케줄만 재등록
#   bash setup_24x7.sh reset    # macOS 기본값으로 복원
#
# 주의:
#   - 어댑터(전원) 연결 상태에서만 의미 있음
#   - 클램셸 모드: 외부 모니터 + 마우스/키보드 연결 필수
# ===============================================================
set -euo pipefail

# ─── 색상 ────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✅${RESET} $*"; }
warn() { echo -e "  ${YELLOW}⚠️ ${RESET} $*"; }
err()  { echo -e "  ${RED}❌${RESET} $*"; }
info() { echo -e "  ${BLUE}ℹ️ ${RESET} $*"; }
hdr()  { echo -e "\n${BOLD}$*${RESET}"; }

# ─── 사전 점검 ───────────────────────────────────────

check_sudo() {
    if [ "$EUID" -ne 0 ]; then
        err "pmset 설정은 sudo가 필요합니다."
        echo ""
        echo "  다시 실행: sudo bash $0 ${CMD:-apply}"
        exit 1
    fi
}

check_power_adapter() {
    hdr "⚡ 전원 상태 확인"
    if pmset -g ps 2>/dev/null | grep -q "AC Power"; then
        ok "어댑터 연결됨 (AC Power) — 24/7 운영 가능"
        return 0
    else
        warn "배터리 전원 상태. 어댑터를 연결해야 안정적인 24/7 운영 가능."
        return 1
    fi
}


check_model() {
    hdr "💻 Mac 모델 확인"
    MODEL=$(system_profiler SPHardwareDataType 2>/dev/null | grep 'Model Name' | awk -F': ' '{print $2}' || echo 'Unknown')
    echo "  모델: $MODEL"
    if echo "$MODEL" | grep -qi "macbook"; then
        info "MacBook 감지 — 클램셸 모드 사용 시 외부 모니터 필요"
    else
        info "모델 확인 — 클램셸 사용 시 외부 모니터 필요"
    fi
    echo ""
}


# ─── pmset 설정 적용 ────────────────────────────────

apply_pmset() {
    hdr "🔧 pmset 절전 설정 적용 (어댑터 연결 시 기준: -c)"

    # -c = AC power (어댑터), -b = battery, -a = all
    # 어댑터 연결 상태에서만 절전 비활성화 (배터리는 건드리지 않음)

    sudo pmset -c sleep          0     && ok "sleep 0 (절전 없음)"
    sudo pmset -c disksleep      0     && ok "disksleep 0 (디스크 절전 없음)"
    sudo pmset -c displaysleep   30    && ok "displaysleep 30분 (화면만 절전, 시스템은 유지)"
    sudo pmset -c hibernatemode  0     && ok "hibernatemode 0 (하이버네이션 비활성)"
    sudo pmset -c autopoweroff   0     && ok "autopoweroff 0 (자동 전원 차단 비활성)"
    sudo pmset -c standby        0     && ok "standby 0 (스탠바이 비활성)"
    sudo pmset -c powernap       0     && ok "powernap 0 (Power Nap 비활성 — pykrx 간섭 방지)"
    sudo pmset -c tcpkeepalive   1     && ok "tcpkeepalive 1 (TCP 연결 유지 — 텔레그램 봇)"

    echo ""
    info "배터리(-b) 설정은 변경하지 않았습니다 (macOS 기본값 유지)."
}


# ─── 클램셸 모드 안내 ────────────────────────────────

guide_clamshell() {
    hdr "🖥  클램셸 모드 (MacBook 뚜껑 닫고 운영)"
    echo ""
    echo "  클램셸 모드 = 뚜껑 닫은 상태로 외부 모니터·어댑터에 연결"
    echo ""
    echo "  필수 조건:"
    echo "    1. 어댑터(MagSafe/USB-C) 연결 ← 필수"
    echo "    2. 외부 모니터 연결 (HDMI/USB-C)"
    echo "    3. 외부 마우스 또는 키보드 연결 (Bluetooth OK)"
    echo ""
    echo "  주의사항:"
    echo "    - 외부 모니터 없이 뚜껑 닫으면 그냥 절전됨"
    echo "    - 위 pmset 설정 후에도 뚜껑 닫기 전 반드시 외부 모니터 깨워둘 것"
    echo "    - 발열 관리: 뚜껑 닫으면 내장 팬 속도 증가. 환기 확보 권장"
    echo ""
    warn "외부 모니터 없이 뚜껑 닫으면 절전 진입"
}


# ─── 주간 wake 스케줄 ────────────────────────────────

apply_wake_schedule() {
    hdr "⏰ 주간 스캔 전 자동 wake 스케줄 등록"

    # pmset repeat: 요일 코드 M=월 T=화 W=수 Th=목 F=금 S=토 Su=일
    # 매주 월요일 08:50 wake (키움봇 09:00 스캔 10분 전 준비)
    sudo pmset repeat wakeorpoweron M 08:50:00

    ok "매주 월요일 08:50 자동 wake 등록"
    info "wakeorpoweron = 절전 해제 OR 전원 차단 상태면 켜기"
    echo ""
    info "추가 wake 등록 예시 (수동):"
    echo "    sudo pmset repeat wakeorpoweron MTWRF 08:50:00   # 평일 매일"
    echo "    sudo pmset -g sched                               # 현재 스케줄 확인"
}


# ─── 상태 점검 ───────────────────────────────────────

cmd_check() {
    hdr "📊 현재 pmset 상태"
    echo ""

    # 주요 항목 파싱
    PMSET=$(pmset -g 2>/dev/null || echo "")

    check_val() {
        local key="$1"; local expect="$2"; local label="$3"
        local val
        # $2 = 값 필드. $NF는 "sleep prevented by powerd)" 같은 주석 끝을 잡으므로 사용 안 함
        val=$(echo "$PMSET" | grep -w "$key" | awk '{print $2}' || echo "N/A")
        if [ "$val" = "$expect" ]; then
            ok "$label = $val"
        else
            warn "$label = $val (권장: $expect)"
        fi
    }

    check_val "sleep"         "0"  "sleep"
    check_val "disksleep"     "0"  "disksleep"
    check_val "hibernatemode" "0"  "hibernatemode"
    check_val "autopoweroff"  "0"  "autopoweroff"
    check_val "standby"       "0"  "standby"
    check_val "powernap"      "0"  "powernap"
    check_val "tcpkeepalive"  "1"  "tcpkeepalive"

    echo ""
    hdr "📅 wake 스케줄"
    pmset -g sched 2>/dev/null || echo "  (없음)"

    echo ""
    hdr "⚡ 전원 상태"
    pmset -g ps 2>/dev/null | head -5

    echo ""
    hdr "🖥  디스플레이 연결"
    system_profiler SPDisplaysDataType 2>/dev/null \
        | grep -E "(Resolution|Display Type|Connection Type)" \
        | head -10 || echo "  (정보 없음)"
}


# ─── macOS 기본값 복원 ───────────────────────────────

cmd_reset() {
    hdr "↩️  pmset macOS 기본값 복원"
    CMD="reset"
    check_sudo

    sudo pmset -c sleep          1
    sudo pmset -c disksleep      10
    sudo pmset -c displaysleep   10
    sudo pmset -c hibernatemode  3
    sudo pmset -c autopoweroff   1
    sudo pmset -c standby        1
    sudo pmset -c powernap       1
    sudo pmset -c tcpkeepalive   1

    # wake 스케줄 전체 삭제
    sudo pmset repeat cancel 2>/dev/null || true

    ok "pmset 기본값 복원 완료"
    ok "반복 wake 스케줄 삭제 완료"
}




# ─── apply 메인 ──────────────────────────────────────

cmd_apply() {
    hdr "🚀 AI Agent 24/7 운영 환경 설정"
    CMD="apply"
    check_sudo
    check_model
    check_power_adapter || true   # 경고만, 중단 안 함
    apply_pmset
    guide_clamshell
    apply_wake_schedule

    echo ""
    hdr "✅ 설정 완료"
    echo ""
    echo "  다음 단계:"
    echo "    bash $0 check               # 설정 확인"
    if true; then
    echo "    외부 모니터 연결 → 뚜껑 닫기  # 클램셸 모드 진입"
    fi
    echo "    bash ~/울트론/ai-agent/scripts/agent_services.sh status"
    echo ""
    echo "  복원:"
    echo "    sudo bash $0 reset"
}


# ─── wake 재등록만 ───────────────────────────────────

cmd_wake() {
    CMD="wake"
    check_sudo
    apply_wake_schedule
}


# ─── main ────────────────────────────────────────────

case "${1:-check}" in
    apply)   cmd_apply   ;;
    check)   cmd_check   ;;
    wake)    cmd_wake    ;;
    reset)   cmd_reset   ;;
    *)
        echo "사용: bash $0 {apply|check|wake|reset}"
        echo ""
        echo "  apply    pmset 절전 비활성 + wake 스케줄 등록 (sudo)"
        echo "  check    현재 pmset 상태 점검"
        echo "  wake     wake 스케줄만 재등록 (sudo)"
        echo "  reset    macOS 기본값 복원 (sudo)"
        ;;
esac
