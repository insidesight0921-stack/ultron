"""proxy_indicators.py — 실시간 대리 지표 (v1, 순수 코어 + 얇은 경계)

계획서 4단계의 "실시간 대리 지표 수집(코스피 200일선 기울기, 외국인 순매수, 환율)".
공식 경기지표(CLI·BSI)는 발표가 한두 달 늦어 **지금 국면**을 말해 주지 못한다.
매일 갱신되는 값으로 그 공백을 메우는 것이 대리 지표의 목적이다.

각 지표는 원값과 함께 **위험선호 방향(risk_on / neutral / risk_off)** 을 낸다.
서로 단위가 달라(포인트·원·억원) 그대로는 합칠 수 없기 때문이다. 다만 방향으로
바꾸는 순간 정보가 줄어드므로 원값도 항상 같이 남긴다.

**모르는 것은 채우지 않는다.** 수집 실패한 지표는 `None`이고, 그 사실이 결과에
남는다(`available` / `missing`). 값을 0이나 직전 값으로 메우면 "중립"이라는 없는
사실이 만들어지고, 그 위에 쌓은 나우캐스팅은 근거 없는 숫자가 된다.

지표별 성격:
  - **코스피 200일선 기울기** — 로컬 일봉 캐시만 쓴다(네트워크 불필요). 가장 안정적.
  - **원/달러 환율** — 원화 약세는 외국인 자금 이탈과 같이 가는 경향. ECOS.
  - **외국인 순매수** — 수급의 직접 신호. pykrx.
  - **VIX** — VKOSPI 대용. 같은 지표가 아니다(미국 시장·다른 기초자산·시차).
    한국 변동성의 대리일 뿐이라는 것을 이름과 설명에 남긴다.
"""
from __future__ import annotations

import logging
from typing import Optional

import env_config

log = logging.getLogger("proxy_indicators")

MA_WINDOW = 200        # 200일선
SLOPE_LOOKBACK = 20    # 기울기를 재는 구간(거래일)

# 방향 판정 임계값 — 모두 **가설**이다. 표본이 쌓이면 재조정한다.
SLOPE_FLAT_PCT = 0.5       # 200일선 20일 변화가 이 안이면 중립(%)
FX_MOVE_PCT = 2.0          # 20일 환율 변화가 이 이상이면 방향으로 본다(%)
VIX_CALM = 18.0
VIX_STRESS = 28.0


# ─── 순수 계산 ───────────────────────────────────────


def moving_average(values: list[float], window: int) -> Optional[float]:
    if not values or len(values) < window:
        return None
    return sum(values[-window:]) / window


def ma_slope_pct(closes: list[float], window: int = MA_WINDOW,
                 lookback: int = SLOPE_LOOKBACK) -> Optional[float]:
    """200일선이 최근 `lookback` 거래일 동안 몇 % 움직였는가(순수).

    수준(현재가가 200일선 위/아래)이 아니라 **기울기**를 본다. 수준은 이미 지나간
    이야기이고, 기울기는 추세가 아직 살아 있는지를 말한다.
    """
    if len(closes) < window + lookback:
        return None
    now = moving_average(closes, window)
    prev = moving_average(closes[:-lookback], window)
    if not now or not prev:
        return None
    return round((now / prev - 1) * 100, 3)


def pct_change(values: list[float], lookback: int) -> Optional[float]:
    if len(values) < lookback + 1 or not values[-lookback - 1]:
        return None
    return round((values[-1] / values[-lookback - 1] - 1) * 100, 3)


def _state(value: Optional[float], *, risk_on_above: float,
           risk_off_below: float) -> str:
    if value is None:
        return "unknown"
    if value >= risk_on_above:
        return "risk_on"
    if value <= risk_off_below:
        return "risk_off"
    return "neutral"


def slope_state(slope: Optional[float]) -> str:
    """200일선이 오르면 위험선호, 내리면 위험회피."""
    return _state(slope, risk_on_above=SLOPE_FLAT_PCT, risk_off_below=-SLOPE_FLAT_PCT)


def fx_state(change_pct: Optional[float]) -> str:
    """**원화 약세(환율 상승)가 위험회피다.** 부호가 뒤집힌다 — 여기서 자주 틀린다."""
    if change_pct is None:
        return "unknown"
    return _state(-change_pct, risk_on_above=FX_MOVE_PCT, risk_off_below=-FX_MOVE_PCT)


def flow_state(net_value: Optional[float]) -> str:
    """외국인 순매수는 그대로 위험선호. 0 근방은 중립으로 두지 않는다 —
    금액은 규모가 제각각이라 임의의 중립 폭을 정할 근거가 없다."""
    if net_value is None:
        return "unknown"
    return "risk_on" if net_value > 0 else ("risk_off" if net_value < 0 else "neutral")


def vix_state(vix: Optional[float]) -> str:
    """VIX가 낮으면 위험선호. 판정 방향이 다른 지표들과 반대라 따로 둔다."""
    if vix is None:
        return "unknown"
    if vix <= VIX_CALM:
        return "risk_on"
    if vix >= VIX_STRESS:
        return "risk_off"
    return "neutral"


def indicator(name: str, value, state: str, *, unit: str = "",
              as_of: Optional[str] = None, source: str = "",
              note: str = "") -> dict:
    return {"name": name, "value": value, "state": state, "unit": unit,
            "as_of": as_of, "source": source, "note": note,
            "available": value is not None}


def summarize(items: list[dict]) -> dict:
    """지표 묶음 → 방향 집계(순수).

    **가중 평균을 내지 않는다.** 서로 단위가 다르고 신뢰도도 다른데 하나의 숫자로
    합치면 그 숫자가 어디서 왔는지 알 수 없게 된다. 방향별 개수와 미확보 목록만 낸다.
    가중 평균(나우캐스팅)은 각 지표의 예측력이 실측으로 확인된 뒤에 얹는다.
    """
    counts = {"risk_on": 0, "neutral": 0, "risk_off": 0, "unknown": 0}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    available = [i for i in items if i["available"]]
    missing = [i["name"] for i in items if not i["available"]]
    lean = "판정 불가"
    if available:
        if counts["risk_on"] > counts["risk_off"]:
            lean = "위험선호 우세"
        elif counts["risk_off"] > counts["risk_on"]:
            lean = "위험회피 우세"
        else:
            lean = "혼조"
    return {"counts": counts, "n_available": len(available),
            "n_total": len(items), "missing": missing, "lean": lean}


def format_snapshot(snap: dict) -> str:
    mark = {"risk_on": "🟢", "neutral": "🟡", "risk_off": "🔴", "unknown": "⚪"}
    s = snap["summary"]
    lines = [f"📡 실시간 대리 지표 — {s['lean']} "
             f"({s['n_available']}/{s['n_total']} 확보)"]
    for i in snap["indicators"]:
        value = "—" if i["value"] is None else f"{i['value']}{i['unit']}"
        line = f"  {mark[i['state']]} {i['name']}: {value}"
        if i["as_of"]:
            line += f"  ({i['as_of']})"
        lines.append(line)
        if i["note"]:
            lines.append(f"      ※ {i['note']}")
    if s["missing"]:
        lines.append(f"  미확보: {', '.join(s['missing'])} — 값을 만들어 채우지 않습니다.")
    if snap.get("missing_env"):
        # "수집 실패"와 "키가 없어서 시도조차 못 함"은 고쳐야 할 곳이 다르다.
        lines.append(f"  ⚙️ 자격 정보 미설정: {', '.join(snap['missing_env'])} "
                     f"(.env 확인 — 수집 실패가 아니라 시도 자체를 못 한 것)")
    lines.append("  ※ 방향 임계값은 가설이며 표본이 쌓이면 재조정합니다.")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def _kospi_series() -> tuple[list[float], Optional[str]]:
    """로컬 KOSPI 일봉 캐시 → (종가, 기준일). 네트워크를 쓰지 않는다."""
    try:
        import json
        import price_sanity as ps
        files = sorted((ps._cache_root() / "indices").glob("market_index_KOSPI_*.json"))
        if not files:
            return [], None
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        return list(payload["series"]["close"]), payload["series"]["date"][-1]
    except Exception as exc:  # noqa: BLE001
        log.warning("KOSPI 캐시 로드 실패: %s", exc)
        return [], None


def _finance_value(name: str) -> tuple[Optional[float], Optional[str]]:
    """finance_bot 지표 → (값, 기준일). **실패는 None으로 돌려주고 이유를 남긴다.**

    `fetch_indicator`는 실패 시 값 없이 `{"error": ...}`를 돌려준다. 처음엔 그 dict를
    성공으로 보고 `float(item.get("value"))`를 불러 `TypeError: ... not 'NoneType'`을
    냈다 — 진짜 원인(API 키 미로딩)이 형변환 오류에 가려졌다. 값이 없으면 오류
    문구를 그대로 남긴다.
    """
    try:
        import finance_bot
        item = finance_bot.fetch_indicator(name)
        if not item:
            return None, None
        if item.get("error"):
            log.warning("%s 조회 실패: %s", name, item["error"])
            return None, None
        value = item.get("value")
        if value is None:
            log.warning("%s: 값이 비어 있음 — 채우지 않는다", name)
            return None, None
        # 키 이름은 `asof`다. `date`/`as_of`를 찾다가 기준일이 늘 비어 있었다.
        return float(value), item.get("asof") or item.get("date")
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 조회 실패: %s", name, exc)
        return None, None


def _foreign_net(days: int = 5) -> tuple[Optional[float], Optional[str]]:
    """최근 N거래일 외국인 순매수 합계(억원). pykrx 필요."""
    try:
        from datetime import datetime, timedelta

        from pykrx import stock
        end = datetime.now()
        start = end - timedelta(days=days * 3 + 10)
        df = stock.get_market_trading_value_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI")
        if df is None or len(df) == 0:
            return None, None
        col = next((c for c in ("외국인합계", "외국인") if c in df.columns), None)
        if col is None:
            return None, None
        recent = df[col].tail(days)
        return round(float(recent.sum()) / 1e8, 1), str(df.index[-1])[:10]
    except Exception as exc:  # noqa: BLE001
        log.warning("외국인 순매수 조회 실패: %s", exc)
        return None, None


REQUIRED_ENV = ("ECOS_API_KEY", "FRED_API_KEY", "KRX_ID", "KRX_PW")
"""이 스냅샷이 쓰는 자격 정보.

  - `ECOS_API_KEY` — 원/달러 환율(한국은행 ECOS)
  - `FRED_API_KEY` — VIX(세인트루이스 연준 FRED)
  - `KRX_ID` / `KRX_PW` — 외국인 순매수. 최근 pykrx는 이 조회에 KRX 계정 로그인을
    요구한다. 없으면 pykrx가 "KRX 로그인 실패"를 낸다.

코스피 200일선 기울기만 로컬 캐시라 키가 필요 없다 — 그래서 키가 하나도 없어도
1/4는 나온다. 그 1/4을 보고 "지표가 도는구나" 하고 넘어가기 쉬워서, 미설정 키를
결과에 명시한다.
"""


def snapshot() -> dict:
    """대리 지표 스냅샷. 실패한 지표는 None으로 남고 그 사실이 결과에 드러난다.

    **`.env`를 먼저 읽는다.** launchd·CLI로 도는 이 스크립트는 봇 프로세스의 환경을
    물려받지 못한다. 첫 실행에서 `.env`에 키가 멀쩡히 있는데도 ECOS·FRED·KRX 셋 다
    "미설정"으로 실패했다.
    """
    missing_env = env_config.ensure_env(REQUIRED_ENV)
    if missing_env:
        log.warning("자격 정보 미설정: %s — 해당 지표는 미확보로 남는다",
                    ", ".join(missing_env))
    closes, kospi_as_of = _kospi_series()
    slope = ma_slope_pct(closes)

    fx, fx_as_of = _finance_value("USD/KRW")
    fx_change = None
    # 환율은 최신값만 받으므로 변화율을 낼 수 없다. 방향은 미확보로 둔다 —
    # 수준만으로 "원화가 약세인가"를 말할 수 없기 때문이다(기준선이 없다).

    vix, vix_as_of = _finance_value("VIX")
    flow, flow_as_of = _foreign_net()

    items = [
        indicator("코스피 200일선 기울기", slope, slope_state(slope), unit="%",
                  as_of=kospi_as_of, source="로컬 일봉 캐시",
                  note=f"최근 {SLOPE_LOOKBACK}거래일 변화"),
        indicator("외국인 순매수(5일)", flow, flow_state(flow), unit="억원",
                  as_of=flow_as_of, source="pykrx"),
        indicator("원/달러 환율", fx, fx_state(fx_change), unit="원",
                  as_of=fx_as_of, source="ECOS",
                  note="수준만 수집됨 — 변화율이 없어 방향은 판정하지 않음"),
        indicator("VIX", vix, vix_state(vix), unit="pt",
                  as_of=vix_as_of, source="FRED",
                  note="VKOSPI 대용. 미국 시장·다른 기초자산·시차가 있어 같은 지표가 아님"),
    ]
    return {"indicators": items, "summary": summarize(items),
            "missing_env": missing_env}


def _cli() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    print(format_snapshot(snapshot()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
