"""param_registry.py — 조정 가능한 파라미터 목록과, 각 값을 판정할 원자료.

**등록 조건은 하나다: 그 값을 판정할 계열이 있어야 한다.** 계열이 없으면
검증이 통과 검사가 아니라 통과 도장이 되고, 그러면 이 창구는 "근거 없는 숫자를
넣는 통로"가 된다 — 이 프로젝트가 하루 종일 걷어낸 바로 그것.

그래서 여기 없는 값들이 있다.
  · 알림 주기·슬롯 비중·종목 수 — 취향이지 실측 대상이 아니다
  · 200일선 기울기 ±0.5% — 계열은 있으나 **부호가 안 바뀌어** 어떤 값도
    검증을 통과할 수 없다(2026-09-01: 143일 최저 +4.79%). 구간이 바뀌면 등록한다
  · VKOSPI 비중 임계(60.6/20.7) — **자기 원장이 따로 있다**
    (`vkospi_threshold_review`, 분기 재측정+승인). 두 곳에서 같은 값을 고치면
    한쪽만 바뀌는 날이 온다
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from param_store import Param

log = logging.getLogger("param_registry")


def _cache_dir(name: str) -> Path:
    import price_sanity as ps

    return ps._cache_root() / name


def _series_from(subdir: str, pattern: str) -> list:
    """캐시 계열의 종가 목록. 없으면 빈 목록 — **채워 넣지 않는다.**"""
    try:
        files = sorted(_cache_dir(subdir).glob(pattern))
        if not files:
            return []
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        return [float(c) for c in (payload.get("series") or {}).get("close") or []]
    except (OSError, ValueError, TypeError) as exc:
        log.warning("%s 계열 로드 실패: %s", subdir, exc)
        return []


def vkospi_series() -> list:
    return _series_from("indices", "market_index_VKOSPI_*.json")


def vix_series() -> list:
    return _series_from("vix", "vix_*.json")


def kospi_window_returns(window: int = 5) -> list:
    """코스피 N거래일 수익률(%) 계열 — 급락 임계값을 판정할 원자료."""
    closes = _series_from("indices", "market_index_KOSPI_*.json")
    if len(closes) <= window:
        return []
    return [(closes[i] / closes[i - window] - 1) * 100
            for i in range(window, len(closes)) if closes[i - window]]


def fx_window_changes(window: int = 20) -> list:
    """원/달러 N거래일 변화율(%) 계열."""
    closes = _series_from("fx", "usdkrw_*.json")
    if len(closes) <= window:
        return []
    return [(closes[i] / closes[i - window] - 1) * 100
            for i in range(window, len(closes)) if closes[i - window]]


# 절대값으로 세면 안 된다 — `Param.two_sided` 주석 참조.


PARAMS: dict[str, Param] = {
    p.key: p for p in (
        Param(key="emergency.vkospi", label="긴급 VKOSPI",
              module="emergency_response", attr="VKOSPI_EMERGENCY",
              kind="rare_high", sample=vkospi_series, unit="pt",
              note="이 위로 진입하면 긴급 경보. 2026-09-01 p90=77.8, 연 1.9회"),
        Param(key="emergency.kospi_drop", label="긴급 코스피 5일 급락",
              module="emergency_response", attr="DROP_EMERGENCY_PCT",
              kind="rare_low", sample=kospi_window_returns, unit="%",
              note="이 아래로 진입하면 긴급 경보. 2026-09-01 p1=−15.0%, 연 2.1회"),
        Param(key="vix.calm", label="VIX 안정(risk_on)",
              module="proxy_indicators", attr="VIX_CALM",
              kind="band_low", sample=vix_series, unit="pt",
              partner="vix.stress",
              note="이 아래면 위험선호. 2026-09-01 실측 58.1% — 치우쳐 있으나 살아 있다"),
        Param(key="vix.stress", label="VIX 스트레스(risk_off)",
              module="proxy_indicators", attr="VIX_STRESS",
              kind="band_high", sample=vix_series, unit="pt",
              partner="vix.calm",
              note="이 위면 위험회피. 2026-09-01 실측 3.4%"),
        Param(key="fx.move", label="환율 20일 변화 임계",
              module="proxy_indicators", attr="FX_MOVE_PCT",
              kind="band_high", sample=fx_window_changes, unit="%",
              two_sided=True,
              note="절대값 기준. 2026-09-01 약세 25.9% / 강세 18.7%. "
                   "**후행 지표라 예측 검정 대상은 아니다**"),
    )
}


def code_default(param: Param) -> float:
    """코드에 적힌 초기값(폴백). 원장이 비면 이 값이 돈다."""
    import importlib

    mod = importlib.import_module(param.module)
    return float(getattr(mod, param.attr))


# ─── 지금 유효한 값 ──────────────────────────────────


def ledger_file():
    from storage_paths import PATHS

    return PATHS.private_state_dir / "params.json"


def active(key: str) -> tuple[float, str]:
    """지금 유효한 (값, 출처). 원장을 못 읽으면 코드 초기값으로 돈다.

    **없는 원장과 깨진 원장을 구분한다.** 없으면 조용히 초기값이지만, 깨졌으면
    경고를 남긴다 — 승인 이력이 사라진 채로 도는 것은 사고다.
    """
    import param_store as store

    param = PARAMS[key]
    fallback = code_default(param)
    path = ledger_file()
    if not path.exists():
        return fallback, "코드 초기값"
    try:
        return store.active_value(store.load(path), param, fallback)
    except Exception as exc:  # noqa: BLE001
        log.warning("파라미터 원장을 읽지 못했다 — 초기값으로 돈다: %s", exc)
        return fallback, "코드 초기값(원장 읽기 실패)"


def active_value(key: str) -> float:
    return active(key)[0]
