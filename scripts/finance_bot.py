#!/usr/bin/env python3
"""
금융봇 (finance_bot) — ECOS(한국은행) + FRED 경제 지표 + 투자 원칙 대조.

4단계 세 번째 도구. 라우터(router.py)가 자연어를 분석해 (action, indicator?)
파라미터를 채워 호출하면 이 모듈이 외부 API에서 최신값을 가져와 응답한다.

설계 결정:
- 외부 키(ECOS_API_KEY, FRED_API_KEY)가 없을 때 친절히 안내. mock 가능하도록
  HTTP 호출은 모듈 함수(_fetch_ecos_raw / _fetch_fred_raw)로 분리 → 테스트는
  monkeypatch로 가짜화.
- 단순 in-memory TTL 캐시 (1시간) — 외부 API rate limit 회피 + 동일 지표를
  같은 세션에서 여러 번 물어볼 때 빠른 응답.
- compare_with_principles(): dashboard 데이터 + wiki RAG 매매 원칙 → 31B에게
  현재 시장이 사용자 원칙과 정합적인지 평가하라고 시킴. (answer, chunks)
  knowledge_bot과 같은 인터페이스.

API:
  fetch_indicator(name) -> dict | None     # latest 단일 지표
  dashboard() -> list[dict]                # 핵심 묶음
  compare_with_principles() -> tuple[str, list[dict]]
  run(action, indicator=None, **k) -> tuple[str, list[dict]]   # 라우터 entrypoint

라우터는 다음 action을 보낸다:
  latest                  단일 지표 (indicator 필수)
  dashboard               핵심 묶음
  compare_with_principles 시장 vs Wiki 원칙 평가
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

# ask.py / knowledge_bot.py와 같은 RAG 함수 재사용
from ask import retrieve, build_context, LLM_MODEL

OLLAMA_URL = "http://127.0.0.1:11434"
KEEP_ALIVE = "30m"

# 캐시 TTL (초). 60분이면 사람이 같은 질문 두 번 해도 같은 값 — OK.
CACHE_TTL_SEC = 60 * 60

log = logging.getLogger("finance_bot")

# ─── 지표 메타 ───────────────────────────────────────


@dataclass(frozen=True)
class Indicator:
    """지표 메타 정보. source ∈ {'ecos', 'fred'}."""
    key: str           # 사용자/라우터가 부르는 이름
    source: str        # 'ecos' or 'fred'
    series: str        # ECOS STAT_CODE 또는 FRED series_id
    item: str | None   # ECOS ITEM_CODE1 (FRED는 None)
    cycle: str         # ECOS 주기: 'D'/'M'/'Q'/'A'
    label: str         # 사용자 표시명
    unit: str          # 단위 표시


# 운용 중인 지표 카탈로그.
# 통계 코드는 한국은행 ECOS / FRED 공식 문서 참조해 보정 가능.
INDICATORS: dict[str, Indicator] = {
    "기준금리":   Indicator("기준금리",   "ecos", "722Y001", "0101000", "M", "한국 기준금리",   "%"),
    "CPI":       Indicator("CPI",        "ecos", "901Y009", "0",       "M", "한국 CPI(전년대비)", "%"),
    "USD/KRW":   Indicator("USD/KRW",    "ecos", "731Y001", "0000001", "D", "원/달러 환율",     "원"),
    "FED":       Indicator("FED",        "fred", "FEDFUNDS", None,     "M", "미국 기준금리",    "%"),
    "DGS10":     Indicator("DGS10",      "fred", "DGS10",    None,     "D", "미국 10년물 국채", "%"),
    "VIX":       Indicator("VIX",        "fred", "VIXCLS",   None,     "D", "VIX 변동성지수",   "pt"),
}

# dashboard에 노출할 핵심 묶음 (정렬 순서 유지)
DASHBOARD_KEYS = ["KOSPI?", "USD/KRW", "기준금리", "CPI", "FED", "DGS10", "VIX"]
# KOSPI는 pykrx 사용. 별도 외부 키 없음. 다만 sandbox에서 pykrx 호출 어려움 →
# dashboard에서 KOSPI는 best-effort: 키 'KOSPI?' 매칭 안 되면 자동 스킵.

# ─── 캐시 ────────────────────────────────────────────


_CACHE: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> dict | None:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: dict) -> None:
    _CACHE[key] = (time.time(), val)


def clear_cache() -> None:
    """테스트·운영에서 캐시 강제 무효화."""
    _CACHE.clear()


# ─── 외부 API: 분리된 raw fetcher (테스트는 여기를 patch) ─────


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = Request(url, headers={"User-Agent": "finance_bot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_ecos_raw(stat_code: str, item_code: str, cycle: str) -> dict:
    """ECOS API 호출 — 최신 1건. ECOS_API_KEY 없으면 RuntimeError."""
    key = os.getenv("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY 미설정 (.env에 추가 필요)")
    today = datetime.now()
    if cycle == "D":
        start = (today.replace(day=1)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
    elif cycle == "M":
        start = (today.replace(month=max(1, today.month - 6), day=1)).strftime("%Y%m")
        end = today.strftime("%Y%m")
    else:
        start = "2020"
        end = today.strftime("%Y")

    # 안전한 인자 인코딩
    parts = [
        "https://ecos.bok.or.kr/api/StatisticSearch",
        quote(key, safe=""),
        "json",
        "kr",
        "1",
        "100",
        quote(stat_code, safe=""),
        cycle,
        start,
        end,
        quote(item_code, safe=""),
    ]
    url = "/".join(parts)
    return _http_get_json(url)


def _fetch_fred_raw(series_id: str, limit: int = 1) -> dict:
    """FRED API 호출 — 최신 N건. FRED_API_KEY 없으면 RuntimeError."""
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정 (.env에 추가 필요)")
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={quote(series_id, safe='')}"
        f"&api_key={quote(key, safe='')}"
        "&file_type=json"
        "&sort_order=desc"
        f"&limit={int(limit)}"
    )
    return _http_get_json(url)


# ─── 파싱 ────────────────────────────────────────────


def _parse_ecos_latest(payload: dict) -> tuple[float, str]:
    """ECOS 응답 → (값, 시점 문자열). 데이터 없으면 ValueError."""
    rows = (payload.get("StatisticSearch") or {}).get("row") or []
    if not rows:
        raise ValueError("ECOS: 데이터 없음")
    # 가장 최신(time desc) 1건 선택
    rows = sorted(rows, key=lambda r: r.get("TIME", ""), reverse=True)
    row = rows[0]
    raw_val = row.get("DATA_VALUE", "")
    try:
        val = float(str(raw_val).replace(",", ""))
    except ValueError:
        raise ValueError(f"ECOS: 값 파싱 실패: {raw_val!r}")
    return val, row.get("TIME", "?")


def _parse_fred_latest(payload: dict) -> tuple[float, str]:
    obs = payload.get("observations") or []
    if not obs:
        raise ValueError("FRED: 데이터 없음")
    row = obs[0]
    raw_val = row.get("value", "")
    if raw_val in ("", ".", "NA"):
        raise ValueError(f"FRED: 결측치 ('{raw_val}')")
    try:
        val = float(raw_val)
    except ValueError:
        raise ValueError(f"FRED: 값 파싱 실패: {raw_val!r}")
    return val, row.get("date", "?")


# ─── 공개 API: 단일 지표 ─────────────────────────────


def fetch_indicator(name: str) -> dict | None:
    """지표 최신값 조회. 카탈로그에 없으면 None.

    반환 dict:
      {key, label, value, unit, asof, source, cached: bool}
    실패 시 {key, label, error: "..."} 형태 (호출 측이 표시).
    """
    ind = INDICATORS.get(name)
    if ind is None:
        # 별칭 보정 — 케이스 무시
        for k, v in INDICATORS.items():
            if k.lower() == name.lower():
                ind = v
                break
    if ind is None:
        return None

    cached = _cache_get(ind.key)
    if cached:
        return {**cached, "cached": True}

    try:
        if ind.source == "ecos":
            payload = _fetch_ecos_raw(ind.series, ind.item or "", ind.cycle)
            val, asof = _parse_ecos_latest(payload)
        elif ind.source == "fred":
            payload = _fetch_fred_raw(ind.series, limit=1)
            val, asof = _parse_fred_latest(payload)
        else:
            return {"key": ind.key, "label": ind.label, "error": f"미지원 source: {ind.source}"}
    except Exception as e:
        log.warning(f"지표 조회 실패 [{ind.key}]: {e}")
        return {"key": ind.key, "label": ind.label, "error": str(e)}

    out = {
        "key": ind.key,
        "label": ind.label,
        "value": val,
        "unit": ind.unit,
        "asof": asof,
        "source": ind.source,
        "cached": False,
    }
    _cache_set(ind.key, out)
    return out


# ─── 공개 API: dashboard ─────────────────────────────


def dashboard(parallel: bool = True, max_workers: int = 6) -> list[dict]:
    """핵심 지표 묶음. 실패한 지표도 포함(error 필드).

    parallel=True (기본): ThreadPoolExecutor로 모든 지표 동시 fetch.
                          캐시 hit는 즉시 통과 (lock 없이 dict 읽기 = GIL 보호).
                          외부 HTTP 6개가 직렬 ~1.5초 → 병렬 ~0.3초.
    parallel=False:        직렬 (테스트·디버깅용).
    """
    keys = list(INDICATORS.keys())
    if not parallel or len(keys) <= 1:
        return [item for item in (fetch_indicator(k) for k in keys) if item is not None]

    # 병렬 호출 — 결과를 INDICATORS 등록 순서대로 정렬해서 반환 (UX 일관성)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_indicator, k): k for k in keys}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                item = fut.result()
            except Exception as e:
                log.warning(f"dashboard 병렬 fetch 예외 [{k}]: {e}")
                item = {"key": k, "label": INDICATORS[k].label, "error": str(e)}
            if item is not None:
                results[k] = item
    return [results[k] for k in keys if k in results]


def _format_indicator_line(item: dict) -> str:
    lab = item["label"]
    if "error" in item:
        return f"• {lab}: ⚠️ {item['error']}"
    cached_flag = " (cached)" if item.get("cached") else ""
    return f"• {lab}: {item['value']:.4g}{item['unit']} (기준 {item['asof']}){cached_flag}"


def format_dashboard(items: list[dict]) -> str:
    if not items:
        return "📉 가져온 지표 없음 — ECOS_API_KEY / FRED_API_KEY 확인."
    lines = ["📊 경제 지표 대시보드"]
    lines.extend(_format_indicator_line(i) for i in items)
    return "\n".join(lines)


# ─── 공개 API: 투자 원칙 대조 ───────────────────────


PRINCIPLES_QUERIES = [
    "매매 진입 조건",
    "리스크 관리 원칙",
    "모멘텀 크래시 신호",
    "수급 판단 기준",
    "분산 슬롯 비율",
]


def _gather_principles(k_each: int = 2) -> list[dict]:
    """위 쿼리들로 RAG 검색해 청크 모으기 (중복 파일 dedup)."""
    seen: set[tuple[str, str]] = set()
    chunks: list[dict] = []
    for q in PRINCIPLES_QUERIES:
        try:
            res = retrieve(q, k=k_each)
        except Exception as e:
            log.warning(f"RAG 검색 실패({q}): {e}")
            continue
        for c in res:
            sig = (c.get("file", ""), c.get("section", ""))
            if sig in seen:
                continue
            seen.add(sig)
            chunks.append(c)
    return chunks


COMPARE_SYSTEM_PROMPT = """당신은 현준의 투자 비서입니다.

지금부터 [현재 시장 지표]와 [현준의 투자 원칙 노트]가 주어집니다.
두 정보만 근거로 다음 항목을 한국어로 간결하게 평가하세요:

1. 어떤 원칙이 현재 트리거되거나 임박했는가?
2. 어떤 원칙이 현재와 명백히 충돌하는가?
3. 결론: 현재 포지션을 점검할 필요가 있는가? (Yes/No + 근거 한 줄)

규칙:
- 노트와 지표에 없는 사실은 추측 금지.
- 결정·매매 권유는 하지 말 것 (점검 트리거만).
- 답변에 [출처] 표기는 넣지 마세요. 시스템이 따로 표시합니다.
"""


def compare_with_principles() -> tuple[str, list[dict]]:
    """대시보드 지표 + Wiki 원칙 → 31B 통합 평가. (answer, chunks)."""
    items = dashboard()
    indicators_text = format_dashboard(items)

    chunks = _gather_principles(k_each=2)
    if not chunks:
        return (
            indicators_text + "\n\n⚠️ wiki에서 원칙 노트를 찾지 못했습니다.",
            [],
        )

    context = build_context(chunks)
    user_prompt = (
        f"[현재 시장 지표]\n{indicators_text}\n\n"
        f"---\n\n"
        f"[현준의 투자 원칙 노트]\n{context}\n\n"
        f"---\n\n"
        "위 두 정보를 대조해 평가해 주세요."
    )

    body = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": COMPARE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0.3, "num_ctx": 32768},
        }
    ).encode("utf-8")
    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.exception("compare_with_principles LLM 호출 실패")
        return (f"{indicators_text}\n\n❌ LLM 평가 실패: {e}", chunks)

    answer = (resp.get("message", {}).get("content") or "").strip()
    final = f"{indicators_text}\n\n---\n\n{answer}"
    return (final, chunks)


# ─── 라우터 entrypoint ───────────────────────────────


def run(
    action: str,
    indicator: str | None = None,
    **kwargs,
) -> tuple[str, list[dict]]:
    """라우터에서 호출하는 단일 entrypoint."""
    action = (action or "").strip().lower()

    if action == "latest":
        if not indicator:
            return ("❌ 'latest'는 indicator 인자가 필요합니다 (예: USD/KRW, VIX, 기준금리).", [])
        item = fetch_indicator(indicator)
        if item is None:
            available = ", ".join(INDICATORS.keys())
            return (
                f"❌ 알 수 없는 지표: {indicator!r}\n"
                f"지원 지표: {available}",
                [],
            )
        return (_format_indicator_line(item), [])

    if action == "dashboard":
        items = dashboard()
        return (format_dashboard(items), [])

    if action == "compare_with_principles":
        return compare_with_principles()

    return (
        f"❌ 알 수 없는 action: {action!r} (latest/dashboard/compare_with_principles)",
        [],
    )


# ─── CLI ─────────────────────────────────────────────


def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_lat = sub.add_parser("latest")
    p_lat.add_argument("indicator")
    sub.add_parser("dashboard")
    sub.add_parser("compare")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "latest":
        msg, _ = run("latest", indicator=args.indicator)
        print(msg)
    elif args.cmd == "dashboard":
        msg, _ = run("dashboard")
        print(msg)
    elif args.cmd == "compare":
        msg, srcs = run("compare_with_principles")
        print(msg)
        if srcs:
            print("\n📚 참조한 노트")
            for c in srcs:
                print(f"  · {c['file']} :: {c.get('section', '')}")


if __name__ == "__main__":
    _cli()
