#!/usr/bin/env python3
"""
.env 파일 + 외부 API 키 검증 스크립트
- .env 존재/포맷 확인
- DART API 키 실제 호출 테스트
- 향후 다른 키(KIS, ECOS 등) 추가 시 같은 패턴으로 확장

사용:
    python3 check_env.py
또는:
    bash check_env.sh
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# ai-agent/.env 위치 자동 탐지 (스크립트 위치 기준)
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR.parent / ".env"  # scripts/ 상위 폴더의 .env


def load_env(path: Path) -> dict[str, str]:
    """간단한 .env 파서 — python-dotenv 의존 없음"""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def mask(s: str) -> str:
    """키 마스킹 — 처음 4자 + 마지막 4자만 노출"""
    if not s:
        return "(empty)"
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


def check_dart(key: str) -> tuple[bool, str]:
    """DART API 호출 테스트 — 삼성전자(00126380) 1월 공시 1건"""
    params = {
        "crtfc_key": key,
        "corp_code": "00126380",
        "bgn_de": "20250101",
        "end_de": "20250131",
        "page_count": "1",
    }
    url = "https://opendart.fss.or.kr/api/list.json?" + urlencode(params)
    try:
        with urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (URLError, HTTPError) as e:
        return False, f"네트워크 오류: {e}"
    except Exception as e:
        return False, f"예외: {type(e).__name__} - {e}"

    status = data.get("status", "")
    message = data.get("message", "")
    if status == "000":
        n = len(data.get("list", []))
        return True, f"정상 응답 — 삼성전자 2025-01 공시 {n}건 조회 성공"
    return False, f"DART 오류 (status={status}): {message}"


def section(title: str) -> None:
    print()
    print("=" * 50)
    print(title)
    print("=" * 50)


def main() -> int:
    section(".env 파일 검증")
    print(f"경로: {ENV_PATH}")
    if not ENV_PATH.exists():
        print("❌ .env 파일 없음")
        print("   먼저 ai-agent/ 폴더에서 cp .env.example .env 실행 후 키 입력")
        return 1
    print("✅ .env 존재")

    env = load_env(ENV_PATH)
    print(f"   로드된 키 개수: {len(env)}")

    section("DART API 키 검증")
    key = env.get("DART_API_KEY", "").strip()
    if not key or key.lower() in ("", "여기에_실제_키", "your_key_here"):
        print("❌ DART_API_KEY 비어있음 또는 미입력")
        return 1
    print(f"✅ 키 로드됨 — 길이 {len(key)}, 마스킹: {mask(key)}")

    print("\nDART API 호출 테스트 중... (timeout 10s)")
    ok, msg = check_dart(key)
    if ok:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
        print("\n오류 가이드:")
        print("  010 → 키 미등록 (발급 직후엔 5~10분 기다려야 할 수 있음)")
        print("  011 → 키 사용 불가 (계정 상태 확인)")
        print("  020 → 호출 한도 초과 (일일 20,000건)")
        print("  100~ → 파라미터 오류 (스크립트 문제일 가능성)")
        return 1

    section("기타 키 점검 (참고)")
    optional_keys = [
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_TELEGRAM_USER_ID",
        "KIS_APP_KEY",
        "ANTHROPIC_API_KEY",
        "ECOS_API_KEY",
    ]
    for k in optional_keys:
        v = env.get(k, "").strip()
        if v:
            print(f"✅ {k}: {mask(v)}")
        else:
            print(f"⚪ {k}: 미입력 (해당 단계에서 필요)")

    section("결론")
    print("🎉 DART API 키 정상 작동. 파싱 스크립트 단계로 진행 가능.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
