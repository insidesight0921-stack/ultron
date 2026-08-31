"""krx_openapi.py — KRX Data Marketplace OPEN API 클라이언트 (얇은 경계 + 순수 파싱)

**왜 필요한가.** VKOSPI(코스피200 변동성지수)에 검증된 무인증 수집 소스가 없어
`market_data_collector`가 수집을 거부하고 있다. `compute_weight_recommendation`의
VKOSPI 항(>30 → 채권 +10%p, <15 → 주식 +10%p)이 그래서 죽어 있다.

**아직 확정되지 않은 것.** KRX OPEN API 서비스 목록(공개 페이지)에 실린 지수
API는 KRX/KOSPI/KOSDAQ 시리즈와 채권지수 4종이고, **변동성지수는 목록에 없다.**
KOSPI 시리즈 응답에 섞여 오는지는 실제로 호출해 봐야 안다.

그래서 이 모듈은 **먼저 탐침(`--probe`)부터 제공한다.** 어느 엔드포인트가
살아 있고 각각 어떤 지수를 돌려주는지 실측한 뒤에 수집기를 붙인다.
이 프로젝트에서 거래소 코드를 추측했다가 **다른 지수를 VKOSPI로 알고 쓴 적이
있다.** 같은 실수를 반복하지 않기 위한 순서다.

규격(2026-08-31 확인, 출처는 계획서에 기록):
    base    http://data-dbg.krx.co.kr
    인증    헤더 `AUTH_KEY`
    파라미터 `basDd` (YYYYMMDD)
    응답    {"OutBlock_1": [ ... ]}
    한도    하루 10,000회

**장비(Linux VM)에서는 호출되지 않는다** — 외부 접속 허용목록에 막힌다.
맥 터미널이나 launchd에서 돌려야 한다.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("krx_openapi")

BASE = os.getenv("KRX_OPENAPI_BASE", "http://data-dbg.krx.co.kr")
KEY_NAME = "KRX_OPENAPI_KEY"
TIMEOUT = 20

# 공개 서비스 목록에 실린 지수 API 4종의 추정 경로.
# **확정이 아니다** — `--probe`가 실제로 어느 것이 200을 주는지 확인한다.
INDEX_ENDPOINTS = {
    "KOSPI 시리즈": "/svc/apis/idx/kospi_dd_trd",
    "KOSDAQ 시리즈": "/svc/apis/idx/kosdaq_dd_trd",
    "KRX 시리즈": "/svc/apis/idx/krx_dd_trd",
    "채권지수": "/svc/apis/idx/bon_dd_trd",
}

# 응답에서 지수 이름·종가로 쓰이는 필드 후보. 실제 이름은 탐침 결과로 확정한다.
NAME_FIELDS = ("IDX_NM", "IDX_NAME", "INDX_NM")
CLOSE_FIELDS = ("CLSPRC_IDX", "CLSPRC", "TDD_CLSPRC")


# ─── 순수 ────────────────────────────────────────────


def pick_field(row: dict, candidates) -> Optional[str]:
    """행에서 후보 중 실제로 있는 필드 이름(순수). 없으면 None."""
    for key in candidates:
        if key in (row or {}):
            return key
    return None


def rows_of(payload: dict) -> list[dict]:
    """응답 → 데이터 행 목록(순수). 블록 이름이 달라져도 첫 리스트를 찾는다."""
    if not isinstance(payload, dict):
        return []
    block = payload.get("OutBlock_1")
    if isinstance(block, list):
        return [r for r in block if isinstance(r, dict)]
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def find_index(rows: list[dict], *keywords: str) -> list[dict]:
    """이름에 키워드가 모두 든 행(순수). 대소문자·공백 무시."""
    out = []
    for row in rows or []:
        field = pick_field(row, NAME_FIELDS)
        if not field:
            continue
        name = str(row.get(field) or "").replace(" ", "").upper()
        if all(str(k).replace(" ", "").upper() in name for k in keywords):
            out.append(row)
    return out


# ─── 경계 (I-O) ──────────────────────────────────────


def _auth_key() -> str:
    key = os.getenv(KEY_NAME, "").strip()
    if not key:
        try:
            import env_config
            env_config.ensure_env([KEY_NAME])
            key = os.getenv(KEY_NAME, "").strip()
        except ImportError:
            pass
    return key


def fetch(path: str, bas_dd: str, *, timeout: int = TIMEOUT) -> dict:
    """엔드포인트 하나 호출. 실패는 예외 대신 `{"error": ...}`로 돌려준다.

    **키가 없으면 호출하지 않는다.** 빈 키로 부르면 서버가 주는 오류가
    '키 없음'인지 '권한 없음'인지 구분되지 않는다.
    """
    key = _auth_key()
    if not key:
        return {"error": f"{KEY_NAME} 미설정 — .env를 확인하세요"}
    url = f"{BASE}{path}?basDd={bas_dd}"
    req = urllib.request.Request(url, headers={"AUTH_KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        return {"error": f"HTTP {e.code}", "body": body}
    except (urllib.error.URLError, OSError) as e:
        return {"error": f"네트워크 실패: {e}"}
    except ValueError as e:
        return {"error": f"JSON 아님: {e}"}


def probe(bas_dd: str) -> dict:
    """어느 지수 엔드포인트가 살아 있고 무엇을 돌려주는지 실측한다.

    반환: {서비스명: {"ok", "n", "fields", "names", "error"}}
    """
    out: dict = {}
    for label, path in INDEX_ENDPOINTS.items():
        payload = fetch(path, bas_dd)
        if payload.get("error"):
            out[label] = {"ok": False, "error": payload["error"],
                          "body": payload.get("body", "")}
            continue
        rows = rows_of(payload)
        field = pick_field(rows[0], NAME_FIELDS) if rows else None
        out[label] = {
            "ok": True, "n": len(rows),
            "fields": sorted(rows[0].keys()) if rows else [],
            "name_field": field,
            "names": [str(r.get(field)) for r in rows] if field else [],
        }
    return out


def format_probe(result: dict, bas_dd: str) -> str:
    """탐침 결과 요약 — **VKOSPI가 어디에 있는지(또는 없는지)를 분명히 말한다.**"""
    lines = [f"🔎 KRX OPEN API 지수 탐침 (basDd={bas_dd})", ""]
    found = []
    for label, r in result.items():
        if not r.get("ok"):
            lines.append(f"❌ {label}: {r.get('error')} {r.get('body','')}".rstrip())
            continue
        lines.append(f"✅ {label}: {r['n']}개 지수 · 이름필드 {r.get('name_field')}")
        hits = [n for n in r.get("names", [])
                if "변동성" in n or "VKOSPI" in n.upper()]
        if hits:
            found.append((label, hits))
            lines.append(f"    🎯 변동성 관련: {', '.join(hits)}")
        if r.get("names"):
            head = ", ".join(r["names"][:8])
            lines.append(f"    지수 예: {head}{' …' if r['n'] > 8 else ''}")
        if r.get("fields"):
            lines.append(f"    필드: {', '.join(r['fields'])}")
    lines.append("")
    if found:
        lines.append("→ 변동성지수가 있습니다. 위 서비스·지수명으로 수집기를 붙이면 됩니다.")
    else:
        lines.append("→ **어느 응답에도 변동성지수가 없습니다.** VKOSPI는 이 API로는")
        lines.append("  받을 수 없다는 뜻이므로, 다른 소스를 찾거나 규칙에서 빼야 합니다.")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    from datetime import date, timedelta

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true", help="지수 엔드포인트 실측")
    ap.add_argument("--date", help="기준일 YYYYMMDD (기본: 어제)")
    args = ap.parse_args()

    bas = args.date or (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    if not _auth_key():
        print(f"❌ {KEY_NAME}가 없습니다. ~/울트론/ai-agent/.env를 확인하세요.")
        return 1
    if args.probe:
        print(format_probe(probe(bas), bas))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
