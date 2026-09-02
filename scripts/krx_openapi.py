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

# 지수 API. **2026-08-31 로그인 후 화면에서 API ID를 직접 확인해 확정했다.**
#
# 처음엔 이름 규칙으로 4종을 유추했고 넷 다 맞았지만, **`drvprod_dd_trd`
# (파생상품지수)를 통째로 빠뜨렸다.** 하필 그게 VKOSPI 최유력 후보다 —
# 코스피200 변동성지수는 옵션에서 산출되는 **파생상품지수**이고, 공개
# 서비스 목록 페이지에는 이 항목이 렌더링되지 않아 보이지 않았다.
#
# **목록을 유추로 만들면 없는 것을 없다고 말하게 된다.** 설령 401이 아니라
# 200이 왔더라도, 이 엔드포인트를 안 불렀으면 "변동성지수 없음"이라는
# 오답이 나왔을 것이다.
#
# 활용신청 승인분(2026-09-01~2027-08-31)만 남긴다. 채권지수·KOSDAQ 시리즈는
# 신청하지 않았으므로 부르지 않는다 — 401을 섞어 놓으면 판정이 흐려진다.
INDEX_ENDPOINTS = {
    "파생상품지수": "/svc/apis/idx/drvprod_dd_trd",   # ← VKOSPI 최유력
    "KOSPI 시리즈": "/svc/apis/idx/kospi_dd_trd",
    "KRX 시리즈": "/svc/apis/idx/krx_dd_trd",
}

# 승인은 받았지만 지수 탐침 대상은 아닌 것들(필요해질 때 붙인다).
OTHER_ENDPOINTS = {
    "유가증권 일별매매": "/svc/apis/sto/stk_bydd_trd",
    "코스닥 일별매매": "/svc/apis/sto/ksq_bydd_trd",
    "유가증권 종목기본": "/svc/apis/sto/stk_isu_base_info",
    "코스닥 종목기본": "/svc/apis/sto/ksq_isu_base_info",
    "ETF 일별매매": "/svc/apis/etp/etf_bydd_trd",
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


def probe_verdict(result: dict) -> str:
    """탐침 결과의 판정(순수): found | absent | unknown

    **`unknown`이 핵심이다.** 2026-08-31: 네 엔드포인트가 전부 401로 실패했는데
    이 함수의 첫 구현이 "변동성지수가 없다"고 단정했다. 호출이 실패했으면
    **아무것도 확인하지 못한 것**이다. 실패한 측정에서 결론을 내는 것이 이
    프로젝트에서 반복된 오류다.
    """
    oks = [r for r in (result or {}).values() if r.get("ok")]
    if not oks:
        return "unknown"
    for r in oks:
        if any("변동성" in n or "VKOSPI" in str(n).upper()
               for n in r.get("names", [])):
            return "found"
    return "absent"


def auth_hint(result: dict) -> str:
    """401이 무엇을 뜻하는지(순수). 아니면 빈 문자열.

    KRX는 **인증키 발급과 API별 이용 신청이 따로**다. 키만 받고 서비스를
    신청하지 않으면 호출이 401로 거절된다. 응답이 KRX 형식(respCode)으로
    돌아온다는 것은 요청이 게이트웨이까지 닿았다는 뜻이므로, base URL·경로·
    헤더 이름이 아니라 **권한** 쪽 문제일 가능성이 높다.
    """
    bodies = [str(r.get("body") or "") for r in (result or {}).values()
              if not r.get("ok")]
    codes = [str(r.get("error") or "") for r in (result or {}).values()
             if not r.get("ok")]
    if not any("401" in c for c in codes):
        return ""
    reached = any("respCode" in b or "respMsg" in b for b in bodies)
    lines = ["401 Unauthorized — 키는 읽혔지만 호출이 거절됐습니다."]
    if reached:
        lines.append("응답이 KRX 형식(respCode)으로 왔으므로 요청은 서버까지 "
                     "닿았습니다 — 주소·경로·헤더 이름 문제는 아닐 가능성이 높습니다.")
    lines.append("KRX는 **인증키 발급**과 **API별 이용 신청**이 따로입니다. "
                 "openapi.krx.co.kr 로그인 → 서비스 이용 → 지수에서 "
                 "쓰려는 API를 각각 신청했는지 확인하세요(승인까지 시간이 걸립니다).")
    return "\n".join(lines)


def format_probe(result: dict, bas_dd: str) -> str:
    """탐침 결과 요약.

    **판정 세 가지를 구분한다** — 있다 / 없다 / 확인 못 했다.
    호출이 전부 실패했는데 '없다'고 적으면, 실패한 측정에서 결론을 내는 것이다.
    """
    lines = [f"🔎 KRX OPEN API 지수 탐침 (basDd={bas_dd})", ""]
    for label, r in result.items():
        if not r.get("ok"):
            lines.append(f"❌ {label}: {r.get('error')} {r.get('body','')}".rstrip())
            continue
        lines.append(f"✅ {label}: {r['n']}개 지수 · 이름필드 {r.get('name_field')}")
        hits = [n for n in r.get("names", [])
                if "변동성" in n or "VKOSPI" in str(n).upper()]
        if hits:
            lines.append(f"    🎯 변동성 관련: {', '.join(hits)}")
        if r.get("names"):
            head = ", ".join(r["names"][:8])
            lines.append(f"    지수 예: {head}{' …' if r['n'] > 8 else ''}")
        if r.get("fields"):
            lines.append(f"    필드: {', '.join(r['fields'])}")
    lines.append("")

    verdict = probe_verdict(result)
    if verdict == "found":
        lines.append("→ 변동성지수가 있습니다. 위 서비스·지수명으로 수집기를 붙이면 됩니다.")
    elif verdict == "absent":
        lines.append("→ 응답은 받았지만 **어느 응답에도 변동성지수가 없습니다.**")
        lines.append("  이 API로는 받을 수 없다는 뜻이므로 다른 소스를 찾아야 합니다.")
    else:
        lines.append("→ ⚠️ **판정 불가 — 호출이 전부 실패했습니다.**")
        lines.append("  VKOSPI가 있는지 없는지 **아직 아무것도 확인하지 못했습니다.**")
        hint = auth_hint(result)
        if hint:
            lines.append("")
            for line in hint.splitlines():
                lines.append(f"  {line}")
    return "\n".join(lines)



# ─── 인증 진단 (음성 대조) ───────────────────────────
#
# **"헤더 이름이 틀렸나, 권한이 없나"는 추측으로 가릴 수 없다.** 둘 다 401을
# 준다. 그래서 **음성 대조**를 쓴다 — 키를 아예 안 보낸 요청, 엉터리 키를 보낸
# 요청과 응답을 비교한다.
#
#   진짜 키 == 엉터리 키 == 키 없음   → 서버가 키를 안 보고 있다(헤더 이름 또는 권한)
#   진짜 키 != 엉터리 키              → 헤더는 읽히고 있다. 키/권한 문제다
#
# 이 프로젝트에서 음성 대조가 검사 자체의 결함을 세 번 잡아냈다(순열검정 2회,
# 전후반 기준선 1회). 같은 도구를 여기에도 쓴다.

AUTH_VARIANTS = [
    ("헤더 AUTH_KEY", "header", "AUTH_KEY"),
    ("헤더 auth_key", "header", "auth_key"),
    ("헤더 authKey", "header", "authKey"),
    ("헤더 apiKey", "header", "apiKey"),
    ("헤더 Authorization: Bearer", "bearer", "Authorization"),
    ("쿼리 AUTH_KEY", "query", "AUTH_KEY"),
]

GARBAGE_KEY = "0" * 40


def _call_variant(path: str, bas_dd: str, kind: str, name: str,
                  key: str, *, timeout: int = TIMEOUT) -> dict:
    """인증 방식 하나로 호출해 (상태, 본문)만 돌려준다."""
    url = f"{BASE}{path}?basDd={bas_dd}"
    headers = {}
    if key:
        if kind == "header":
            headers[name] = key
        elif kind == "bearer":
            headers[name] = f"Bearer {key}"
        elif kind == "query":
            url += f"&{name}={key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return {"status": resp.status if hasattr(resp, "status") else 200,
                "body": body[:200]}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            body = ""
        return {"status": e.code, "body": body}
    except (urllib.error.URLError, OSError) as e:
        return {"status": None, "body": f"네트워크 실패: {e}"}


def auth_diagnose(bas_dd: str, path: Optional[str] = None) -> dict:
    """인증 방식별 응답 + 음성 대조. 반환: {variants, controls}"""
    path = path or INDEX_ENDPOINTS["KOSPI 시리즈"]
    key = _auth_key()
    if not key:
        return {"error": f"{KEY_NAME} 미설정"}
    out = {"path": path, "variants": {}, "controls": {}}
    for label, kind, name in AUTH_VARIANTS:
        out["variants"][label] = _call_variant(path, bas_dd, kind, name, key)
    out["controls"]["키 없음"] = _call_variant(path, bas_dd, "header", "AUTH_KEY", "")
    out["controls"]["엉터리 키"] = _call_variant(
        path, bas_dd, "header", "AUTH_KEY", GARBAGE_KEY)
    return out


# KRX가 401에 실어 보내는 두 메시지. **뜻이 다르다** — 2026-08-31 실측으로 확인.
#
#   "Unauthorized Key"      키 자체를 못 알아본다(키 없음·엉터리 키가 이 응답)
#   "Unauthorized API Call" 키는 알아봤는데 **이 API 호출이 허용되지 않았다**
#
# 즉 후자가 나오면 키는 유효하고, 막힌 것은 **그 API에 대한 활용신청/승인**이다.
MSG_BAD_KEY = "Unauthorized Key"
MSG_NOT_SUBSCRIBED = "Unauthorized API Call"


def auth_verdict(diag: dict) -> str:
    """진단 판정(순수): ok | not_subscribed | key_read | key_ignored | unknown

    ok             어떤 방식이든 200을 받았다
    not_subscribed 키는 인식됐는데 **이 API가 허용되지 않았다**(활용신청 미승인)
    key_read       진짜 키와 엉터리 키의 응답이 다르다 → 서버가 키를 보고 있다
    key_ignored    진짜 키·엉터리 키·키 없음이 모두 같다 → 키가 반영되지 않는다
    unknown        네트워크 실패 등으로 비교 자체가 불가
    """
    variants = (diag or {}).get("variants") or {}
    controls = (diag or {}).get("controls") or {}
    if not variants or not controls:
        return "unknown"
    if any(v.get("status") == 200 for v in variants.values()):
        return "ok"
    if any(v.get("status") is None for v in variants.values()):
        return "unknown"
    real = variants.get("헤더 AUTH_KEY") or {}
    garbage = controls.get("엉터리 키") or {}
    none = controls.get("키 없음") or {}
    same = (real.get("status"), real.get("body")) == \
           (garbage.get("status"), garbage.get("body")) == \
           (none.get("status"), none.get("body"))
    if same:
        return "key_ignored"
    # 키는 읽혔다. 메시지가 '이 API 호출이 허용되지 않음'이면 원인이 특정된다.
    if (MSG_NOT_SUBSCRIBED in str(real.get("body") or "")
            and MSG_BAD_KEY in str(garbage.get("body") or "")):
        return "not_subscribed"
    return "key_read"


def working_auth_styles(diag: dict) -> list[str]:
    """키가 인식된 인증 방식들(순수). 대조군과 다른 응답을 낸 것들.

    어느 헤더가 맞는지 **실측으로** 알 수 있다 — 문서를 다시 뒤질 필요가 없다.
    """
    controls = (diag or {}).get("controls") or {}
    bad = {(c.get("status"), c.get("body")) for c in controls.values()}
    return [label for label, r in ((diag or {}).get("variants") or {}).items()
            if (r.get("status"), r.get("body")) not in bad]


def format_auth_diagnose(diag: dict) -> str:
    """진단 결과 — **무엇이 원인이 아닌지**까지 말한다."""
    if diag.get("error"):
        return f"❌ {diag['error']}"
    lines = [f"🔐 인증 방식 진단 ({diag['path']})", "",
             "인증 방식별 응답:"]
    for label, r in diag["variants"].items():
        lines.append(f"  {label:26} → {r['status']} {r['body'][:80]}")
    lines.append("")
    lines.append("음성 대조:")
    for label, r in diag["controls"].items():
        lines.append(f"  {label:26} → {r['status']} {r['body'][:80]}")
    lines.append("")

    verdict = auth_verdict(diag)
    if verdict == "ok":
        ok = [l for l, r in diag["variants"].items() if r.get("status") == 200]
        lines.append(f"→ ✅ 성공한 방식: {', '.join(ok)}")
    elif verdict == "not_subscribed":
        styles = working_auth_styles(diag)
        lines.append("→ **키는 유효합니다. 이 API에 대한 권한만 없습니다.**")
        lines.append(f"  키를 보냈을 때: \"{MSG_NOT_SUBSCRIBED}\" (호출이 허용되지 않음)")
        lines.append(f"  키가 없거나 틀릴 때: \"{MSG_BAD_KEY}\" (키를 못 알아봄)")
        lines.append("  **두 메시지가 다르다는 것이 키가 인식됐다는 증거입니다.**")
        if styles:
            lines.append(f"  키가 전달된 방식: {', '.join(styles)}")
        lines.append("")
        lines.append("  → 남은 것은 **API별 활용신청**입니다. openapi.krx.co.kr →")
        lines.append("    서비스 이용 → 지수 → 쓰려는 API마다 '활용신청' → 승인 대기.")
        lines.append("    인증키 발급과 활용신청은 별개이고, 승인까지 시간이 걸립니다.")
    elif verdict == "key_read":
        styles = working_auth_styles(diag)
        lines.append("→ **서버가 키를 읽고 있습니다.** 진짜 키와 엉터리 키의 응답이")
        lines.append("  다릅니다. 헤더 이름 문제가 아니라 **키 또는 권한** 문제입니다.")
        if styles:
            lines.append(f"  키가 전달된 방식: {', '.join(styles)}")
        lines.append("  → 인증키 승인 상태와 **API별 활용신청** 승인을 확인하세요.")
    elif verdict == "key_ignored":
        lines.append("→ **키가 응답에 아무 영향을 주지 않습니다.**")
        lines.append("  진짜 키 · 엉터리 키 · 키 없음이 모두 같은 응답입니다.")
        lines.append("  두 가지 중 하나입니다:")
        lines.append("   ① 이 엔드포인트를 **활용신청하지 않아** 인증 이전에 거절된다")
        lines.append("   ② 인증 방식이 위 6가지 중에 없다(KRX 샘플 코드 확인 필요)")
        lines.append("  ①이 훨씬 흔합니다 — KRX는 키 발급과 API별 활용신청이 따로입니다.")
    else:
        lines.append("→ ⚠️ 판정 불가 — 네트워크 실패가 섞여 비교할 수 없습니다.")
    return "\n".join(lines)


def _cli() -> int:
    import argparse
    from datetime import date, timedelta

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true", help="지수 엔드포인트 실측")
    ap.add_argument("--auth", action="store_true",
                    help="인증 방식 진단(음성 대조 — 헤더 문제인지 권한 문제인지)")
    ap.add_argument("--date", help="기준일 YYYYMMDD (기본: 어제)")
    ap.add_argument("--factors", action="store_true",
                    help="팩터/스타일 지수 후보를 찾는다(탭 C 검증용)")
    args = ap.parse_args()

    bas = args.date or (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    if not _auth_key():
        print(f"❌ {KEY_NAME}가 없습니다. ~/울트론/ai-agent/.env를 확인하세요.")
        return 1
    if args.auth:
        print(format_auth_diagnose(auth_diagnose(bas)))
        return 0
    if args.factors:
        # 모든 지수 서비스를 훑어 후보를 모은다 — 어느 서비스에 있는지 모른다.
        allrows, total = [], 0
        for label, path in INDEX_ENDPOINTS.items():
            payload = fetch(path, bas)
            if payload.get("error"):
                print(f"  {label}: 조회 실패 ({payload['error']})")
                continue
            rows = rows_of(payload)
            total += len(rows)
            allrows.extend(rows)
            print(f"  {label}: {len(rows)}개 지수")
        print()
        print(format_factor_candidates(find_factor_indices(allrows), total=total))
        return 0
    if args.probe:
        print(format_probe(probe(bas), bas))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


# ─── VKOSPI (2026-09-01 실측 확정) ───────────────────
#
# 탐침 결과: `파생상품지수`(drvprod_dd_trd) 320개 중 이름이 정확히
# **"코스피 200 변동성지수"** 인 행이 VKOSPI다.
#
# **키워드 매칭으로 고르면 안 된다.** 같은 응답에 "변동성"이 든 지수가 6개다:
#
#     코스피 200 변동성지수            ← 이것만 VKOSPI
#     코스피 200 현선물 목표변동성 24% 지수
#     KRX 최소변동성지수
#     코스피 200 가치저변동성
#     코스피 200 변동성매칭 양매도지수
#     코스피 200 변동성추세 추종 양매도지수
#
# 뒤 다섯은 전략지수·팩터지수로 성격이 완전히 다르다. 이 프로젝트는 예전에
# **거래소 코드를 추측했다가 다른 지수를 VKOSPI로 알고 쓴 적이 있다** —
# 같은 실수를 부분 문자열 매칭으로 반복하지 않는다.
VKOSPI_SERVICE = "파생상품지수"
VKOSPI_INDEX_NAME = "코스피 200 변동성지수"

# 응답 필드(실측): BAS_DD, IDX_NM, IDX_CLSS, OPNPRC_IDX, HGPRC_IDX,
#                  LWPRC_IDX, CLSPRC_IDX, CMPPREVDD_IDX, FLUC_RT
FIELD_DATE = "BAS_DD"
FIELD_CLOSE = "CLSPRC_IDX"


def _norm_name(value) -> str:
    """지수명 정규화(순수) — 공백만 지운다. 글자는 건드리지 않는다."""
    return str(value or "").replace(" ", "")


# 팩터/스타일 지수를 찾을 때 쓰는 단서. **부분일치로 고르지 않는다** —
# 이름에 '변동성'이 든 지수가 6개였던 VKOSPI 사례처럼, 후보를 사람에게
# 보여주고 정확일치로 고른다.
FACTOR_HINTS = {
    "Momentum": ("모멘텀", "momentum"),
    "Value": ("가치", "밸류", "value"),
    "Quality": ("퀄리티", "품질", "quality"),
    "LowVol": ("저변동", "로우볼", "minimum volatility", "low vol"),
    "Size": ("중소형", "소형", "size", "smallcap"),
    "Growth": ("성장", "growth"),
}


def find_factor_indices(rows: list) -> dict:
    """지수 목록에서 팩터 후보를 이름 단서로 추린다(순수).

    **고르지 않는다. 후보를 보여줄 뿐이다.** 어느 것을 쓸지는 사람이 정하고,
    정해진 뒤에는 `pick_index`가 정확일치로 집는다 — 부분일치로 자동 선택하면
    엉뚱한 지수를 집는다(2026-09-01 VKOSPI: '변동성'이 든 지수가 6개였다).
    """
    out: dict = {factor: [] for factor in FACTOR_HINTS}
    for row in rows or []:
        name = None
        for field in NAME_FIELDS:
            if row.get(field):
                name = str(row[field])
                break
        if not name:
            continue
        low = name.lower()
        for factor, hints in FACTOR_HINTS.items():
            if any(h.lower() in low for h in hints):
                out[factor].append(name)
    return {k: sorted(set(v)) for k, v in out.items() if v}


def format_factor_candidates(found: dict, total: int = 0) -> str:
    """사람이 고르라고 내놓는 목록(순수)."""
    lines = [f"🔎 팩터 지수 후보 (전체 {total}개 중)", ""]
    if not found:
        lines.append("  이름 단서로 걸린 지수가 없습니다.")
        lines.append("  KRX가 팩터/스타일 지수를 제공하지 않거나 이름이 다릅니다")
        lines.append("  → 종목 단위로 팩터를 직접 구성해야 합니다(무겁습니다).")
        return "\n".join(lines)
    for factor, names in found.items():
        lines.append(f"  {factor}")
        for n in names[:8]:
            lines.append(f"    · {n}")
        if len(names) > 8:
            lines.append(f"    … 외 {len(names) - 8}개")
    lines.append("")
    lines.append("_후보일 뿐입니다. 어느 것을 쓸지는 사람이 정하고,_")
    lines.append("_정해진 뒤에는 **정확일치**로 집습니다(부분일치 금지)._")
    return "\n".join(lines)


def pick_index(rows: list, name: str) -> Optional[dict]:
    """이름이 **정확히** 일치하는 지수 행(순수). 없으면 None.

    공백 차이만 무시한다("코스피 200 변동성지수" == "코스피200변동성지수").
    부분 일치는 하지 않는다 — 위 6개가 전부 걸린다.
    """
    want = _norm_name(name)
    for row in rows or []:
        field = pick_field(row, NAME_FIELDS)
        if field and _norm_name(row.get(field)) == want:
            return row
    return None


def _to_float(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def vkospi_close(payload: dict, *, name: str = VKOSPI_INDEX_NAME) -> dict:
    """응답 → VKOSPI 종가(순수). 반환: {value, date, found}

    **못 찾으면 값을 만들지 않는다.** 비슷한 이름으로 대체하면 다른 지수를
    VKOSPI로 기록하게 된다.
    """
    rows = rows_of(payload)
    if not rows:
        return {"value": None, "date": None, "found": False,
                "reason": "응답에 데이터 행이 없음"}
    row = pick_index(rows, name)
    if row is None:
        return {"value": None, "date": None, "found": False,
                "reason": f"'{name}' 지수를 찾지 못함 (행 {len(rows)}개) — "
                          f"비슷한 이름으로 대체하지 않는다"}
    value = _to_float(row.get(FIELD_CLOSE))
    if value is None or value <= 0:
        return {"value": None, "date": str(row.get(FIELD_DATE) or ""),
                "found": True, "reason": f"{FIELD_CLOSE} 값이 비어 있음"}
    return {"value": value, "date": str(row.get(FIELD_DATE) or ""),
            "found": True, "reason": ""}


def fetch_vkospi(bas_dd: str) -> dict:
    """하루치 VKOSPI 종가. 실패는 예외 대신 reason으로 돌려준다."""
    payload = fetch(INDEX_ENDPOINTS[VKOSPI_SERVICE], bas_dd)
    if payload.get("error"):
        return {"value": None, "date": bas_dd, "found": False,
                "reason": payload["error"]}
    return vkospi_close(payload)
