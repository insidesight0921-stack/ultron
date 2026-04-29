#!/usr/bin/env bash
# ===============================================================
# AI Agent — Python 환경 세팅
# - Python 3.10+ 자동 탐지 (3.12 권장)
# - venv 생성 (~/울트론/ai-agent/.venv)
# - requirements.txt 일괄 설치
# - 핵심 패키지 import 검증
# ===============================================================
set -euo pipefail

PROJECT="$HOME/울트론/ai-agent"
VENV="$PROJECT/.venv"
REQ="$PROJECT/requirements.txt"

cd "$PROJECT"

# ─── Python 3.10+ 탐지 ────────────────────────────
echo "=========================================="
echo "Python 인터프리터 탐색"
echo "=========================================="

PY_BIN=""
for candidate in python3.12 python3.11 python3.10 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
        VER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PY_BIN=$(command -v "$candidate")
            echo "✅ 사용할 Python: $PY_BIN (v$VER)"
            break
        fi
    fi
done

if [ -z "$PY_BIN" ]; then
    echo "❌ Python 3.10 이상을 찾을 수 없음"
    echo ""
    echo "설치 방법:"
    echo "  brew install python@3.12"
    echo ""
    echo "Homebrew 미설치 시 먼저:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi

# ─── 기존 venv 점검 — 3.9면 삭제 후 재생성 ──────────
if [ -d "$VENV" ]; then
    EXISTING_VER=$("$VENV/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    EX_MAJOR=$(echo "$EXISTING_VER" | cut -d. -f1)
    EX_MINOR=$(echo "$EXISTING_VER" | cut -d. -f2)
    if [ "$EX_MAJOR" -lt 3 ] || [ "$EX_MINOR" -lt 10 ]; then
        echo "⚠️  기존 venv가 Python $EXISTING_VER (구버전) — 삭제 후 재생성"
        rm -rf "$VENV"
    fi
fi

# venv 생성
if [ ! -d "$VENV" ]; then
    echo ""
    echo "venv 생성 중... ($PY_BIN)"
    "$PY_BIN" -m venv "$VENV"
fi

source "$VENV/bin/activate"
echo "✅ venv 활성화: $VENV"
python --version

echo ""
echo "=========================================="
echo "pip + setuptools + wheel 업그레이드"
echo "=========================================="
python -m pip install --upgrade pip wheel setuptools

echo ""
echo "=========================================="
echo "requirements.txt 일괄 설치 (시간 좀 걸림 — 5~10분)"
echo "=========================================="
pip install -r "$REQ"

echo ""
echo "=========================================="
echo "import 검증"
echo "=========================================="
python - <<'PYEOF'
import importlib
modules = [
    ("lancedb", "RAG 벡터 DB"),
    ("pykrx", "한국 주가/수급 데이터"),
    ("pandas_market_calendars", "KRX 거래일 계산"),
    ("watchdog", "파일 변경 감지"),
    ("dotenv", "환경변수 로드"),
    ("fastapi", "모의투자 사이트 백엔드"),
    ("httpx", "비동기 HTTP"),
    ("bs4", "HTML 파싱"),
    ("plotly", "차트"),
    ("anthropic", "Claude API"),
    ("openai", "OpenAI API"),
]
ok, fail = 0, 0
for name, desc in modules:
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "?")
        print(f"  ✅ {name:30s} v{ver:10s} {desc}")
        ok += 1
    except ImportError as e:
        print(f"  ❌ {name:30s} 로드 실패: {e}")
        fail += 1
print(f"\n총 {ok}/{ok+fail} 모듈 정상")
PYEOF

echo ""
echo "=========================================="
echo "✅ 환경 세팅 완료"
echo "=========================================="
echo ""
echo "다음부터 venv 활성화:"
echo "  source ~/울트론/ai-agent/.venv/bin/activate"
echo ""
echo "비활성화:"
echo "  deactivate"
