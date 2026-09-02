"""collect_history.py — 과거 데이터를 소급해서 채운다.

**왜 이 파일이 생겼나(2026-09-01).** 사용자 질문: *"지금 수집하고 있는
데이터를 이전 주가 자료를 보고 수집은 못 하는 거야?"*

확인해 보니 **할 수 있는데 안 하고 있었다.** 하루 종일 "표본이 없다"고
말했는데, 그중 상당수는 없는 게 아니라 **안 가져온 것**이었다.

    VIX          494일 캐시가 있는데 화면은 "로그 1일 — 적재 대기"
    외국인 순매수  pykrx가 임의 기간 조회인데 최근 5일만 부름
    KOSPI 정답    363일만 보유. 수집 상한은 2000일
    나우캐스팅    "하락 국면이 들어오기 전에는 판정할 수 없다"고 결론냈는데,
                 국면은 **기다릴 게 아니라 과거에서 가져오면 되는 것**이었다

**소급 가능한 것과 아닌 것은 다르다.**

    가능   시장이 남긴 기록 — 지수·변동성·환율·수급. 그때 실제로 있었던 값
    불가   우리 시스템이 남겼어야 할 기록 — 봇의 실제 거래, mode 판정,
           사용자 행동. 그때 우리가 없었으므로 지금 만들면 그건 조작이다

이 파일은 **앞의 것만** 채운다. 뒤의 것은 시계를 시작해 기다리는 수밖에 없고,
그 구분이 흐려지면 "과거를 지어내는" 도구가 된다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path


log = logging.getLogger("collect_history")

# 소급 가능한 것들. 각 항목은 (라벨, 캐시 위치, 수집 방법)을 안다.
BACKFILLABLE = ("kospi", "vkospi", "vix", "fx", "flow")


def _cache_root() -> Path:
    import price_sanity as ps

    return ps._cache_root()


def _write_series(subdir: str, name: str, dates: list[str],
                  closes: list[float], *, source: str) -> Path:
    """기존 캐시와 **병합해서** 쓴다.

    2026-09-01 사고: 일간 수집기가 1년치를 20일 창으로 덮었다. 소급 수집도
    같은 실수를 하면 안 된다 — 짧은 창은 갱신용이지 이력의 대체물이 아니다.
    """
    root = _cache_root() / subdir
    root.mkdir(parents=True, exist_ok=True)
    merged: dict[str, float] = {}
    existing = sorted(root.glob(f"{name}_*.json"))
    if existing:
        try:
            prior = json.loads(existing[-1].read_text(encoding="utf-8"))
            series = prior.get("series") or {}
            for d, c in zip(series.get("date") or [], series.get("close") or []):
                merged[str(d)] = float(c)
        except (OSError, ValueError, TypeError):
            log.warning("기존 %s 캐시를 읽지 못해 병합 없이 씁니다", name)
    for d, c in zip(dates, closes):
        merged[str(d)] = float(c)
    days = sorted(merged)
    target = root / f"{name}_{days[-1]}.json"
    target.write_text(json.dumps(
        {"index": name, "source": source, "as_of": days[-1],
         "series": {"date": days, "close": [merged[d] for d in days]}},
        ensure_ascii=False), encoding="utf-8")
    return target


# ─── 항목별 소급 ─────────────────────────────────────


def backfill_kospi(days: int) -> dict:
    """KOSPI 지수 — 정답 시계열의 원천. 국면 전환이 여기 들어 있다."""
    import market_data_collector as mdc

    payload = mdc.collect_market_index("KOSPI", days=days)
    n = len(payload["series"]["date"])
    return {"item": "kospi", "n": n,
            "range": (payload["series"]["date"][0], payload["series"]["date"][-1])}


def backfill_vkospi(days: int) -> dict:
    """VKOSPI — KRX OPEN API는 **하루 한 번씩** 부른다(기간 조회 없음).

    1년치 약 250회. 일 한도 10,000회 안이지만 시간이 걸린다는 점을 호출부가
    알아야 한다.
    """
    import market_data_collector as mdc

    payload = mdc.collect_market_index("VKOSPI", days=days)
    n = len(payload["series"]["date"])
    return {"item": "vkospi", "n": n,
            "range": (payload["series"]["date"][0], payload["series"]["date"][-1])}


def backfill_vix(days: int) -> dict:
    """VIX — FRED는 임의 행 수 조회."""
    import quant_bot as qb

    payload = qb._fetch_fred_series_raw("VIXCLS", months=0, limit=days)
    series = qb._parse_fred_series(payload)
    if not series:
        raise RuntimeError("VIX 응답이 비었습니다.")
    dates = [d.replace("-", "") for d, _ in series]
    closes = [v for _, v in series]
    _write_series("vix", "vix", dates, closes, source="FRED VIXCLS")
    return {"item": "vix", "n": len(dates), "range": (dates[0], dates[-1])}


def backfill_fx(months: int) -> dict:
    """원/달러 — ECOS 임의 기간. 후행 지표지만 동행 설명에는 쓴다."""
    import fx_calibrate as fx
    import quant_bot as qb

    payload = qb._fetch_ecos_series_raw(
        fx.FX_SERIES["stat_code"], fx.FX_SERIES["item_code"], "D", months)
    series = qb._parse_ecos_series(payload)
    if not series:
        raise RuntimeError("원/달러 응답이 비었습니다.")
    dates = [str(d).replace("-", "") for d, _ in series]
    closes = [v for _, v in series]
    _write_series("fx", "usdkrw", dates, closes, source="ECOS 731Y001")
    return {"item": "fx", "n": len(dates), "range": (dates[0], dates[-1])}


KRX_LOGIN_KEYS = ("KRX_ID", "KRX_PW")


def _ensure_krx_login() -> list:
    """`.env`에서 KRX 자격증명을 채우고 **끝내 비어 있는 키**를 돌려준다."""
    try:
        import env_config
        return env_config.ensure_env(KRX_LOGIN_KEYS)
    except ImportError:
        import os
        return [k for k in KRX_LOGIN_KEYS if not os.environ.get(k, "").strip()]


def _flow_failure_reason(error, *, missing=None) -> str:
    """**원인을 정확히 말한다(2026-09-02).**

    이 경로는 KRX 회원 로그인을 요구하는데, 예전 메시지는 「응답이
    비었습니다」였다. 그러면 네트워크나 pykrx를 의심하게 되고 진짜 원인을
    못 찾는다 — CLI 시리즈가 낡았을 때 「키·네트워크 점검」이라 말하던 것과
    같은 실수다.
    """
    import os

    text = str(error or "")
    if missing is None:
        missing = [k for k in KRX_LOGIN_KEYS if not os.environ.get(k, "").strip()]
    if missing:
        return (f"외국인 순매수는 **KRX 회원 로그인이 필요한 경로**입니다"
                f"(pykrx get_market_trading_value_by_date). "
                f"비어 있는 키: {', '.join(missing)} — .env를 확인하세요. "
                f"네트워크·API 키 문제가 아닙니다.")
    if "KRX_ID" in text or "로그인" in text:
        return ("KRX 로그인이 거부됐습니다 — KRX_ID·KRX_PW는 있으나 "
                f"인증에 실패했습니다({text}). 계정·비밀번호를 확인하세요.")
    return f"외국인 순매수 응답이 비었습니다({text or '빈 응답'})."


def backfill_flow(days: int) -> dict:
    """외국인 순매수 — pykrx는 **임의 기간 조회**다.

    `proxy_indicators._foreign_net`이 최근 5일만 부르는 것은 스냅샷 용도라
    그런 것이고, 소급은 여기서 한다. 단위는 억원(원본은 원).
    """
    from pykrx import stock

    # **`.env`를 먼저 읽는다(2026-09-02).** KRX_ID·KRX_PW가 `.env`에 있는데도
    # 「환경 변수가 설정되지 않았습니다」로 실패했다 — 이 스크립트가 `.env`를
    # 읽지 않았기 때문이다. `env_config` 첫 줄에 「같은 원인이 두 번 나왔다」고
    # 적혀 있는데 여기가 **세 번째**였다. 그래서 그 한 곳을 쓴다.
    missing = _ensure_krx_login()

    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6) + 30)   # 휴장일 여유
    if missing:
        raise RuntimeError(_flow_failure_reason(None, missing=missing))
    try:
        df = stock.get_market_trading_value_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "KOSPI")
    except Exception as e:                                  # noqa: BLE001
        raise RuntimeError(_flow_failure_reason(e)) from e
    if df is None or len(df) == 0:
        raise RuntimeError(_flow_failure_reason(None))
    col = next((c for c in ("외국인합계", "외국인") if c in df.columns), None)
    if col is None:
        raise RuntimeError(f"외국인 컬럼을 찾지 못했습니다: {list(df.columns)}")
    dates = [str(d)[:10].replace("-", "") for d in df.index]
    closes = [float(v) / 1e8 for v in df[col]]
    _write_series("flow", "foreign_net", dates, closes, source="pykrx KOSPI")
    return {"item": "flow", "n": len(dates), "range": (dates[0], dates[-1])}


# ─── 국면 점검 (순수) ────────────────────────────────


def regime_span(dates: list[str], closes: list[float],
                window: int = 200) -> dict:
    """이 구간에 **국면 전환이 들어 있는가**(순수).

    나우캐스팅이 막힌 진짜 이유는 표본 수가 아니라 국면이 하나뿐인 것이었다.
    소급 수집의 목적이 이 값을 바꾸는 것이므로, 수집기가 직접 답한다.
    """
    import proxy_indicators as pi

    above = below = 0
    flips = 0
    prev = None
    for i in range(len(closes)):
        ma = pi.moving_average(closes[:i + 1], window)
        if ma is None:
            continue
        state = closes[i] > ma
        above += state
        below += not state
        if prev is not None and state != prev:
            flips += 1
        prev = state
    total = above + below
    return {"n": total, "above": above, "below": below, "flips": flips,
            "below_pct": round(below / total * 100, 1) if total else None,
            "has_both": above > 0 and below > 0}


def _cli() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="과거 데이터 소급 수집")
    ap.add_argument("--kospi", type=int, metavar="DAYS")
    ap.add_argument("--vkospi", type=int, metavar="DAYS")
    ap.add_argument("--vix", type=int, metavar="DAYS")
    ap.add_argument("--fx", type=int, metavar="MONTHS")
    ap.add_argument("--flow", type=int, metavar="DAYS")
    ap.add_argument("--check", action="store_true",
                    help="수집하지 않고 지금 보유량과 국면 포함 여부만 본다")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import env_config
        env_config.ensure_env(["KRX_OPENAPI_KEY", "FRED_API_KEY", "ECOS_API_KEY"])
    except ImportError:
        pass

    if args.check or not any(
            (args.kospi, args.vkospi, args.vix, args.fx, args.flow)):
        print(format_status())
        return 0

    jobs = [("kospi", args.kospi, backfill_kospi),
            ("vkospi", args.vkospi, backfill_vkospi),
            ("vix", args.vix, backfill_vix),
            ("fx", args.fx, backfill_fx),
            ("flow", args.flow, backfill_flow)]
    for name, value, fn in jobs:
        if not value:
            continue
        if name == "vkospi":
            print(f"VKOSPI {value}일 — 하루에 한 번씩 부릅니다(약 {value}회 호출)…")
        else:
            print(f"{name} 소급 수집 중…")
        try:
            got = fn(value)
            print(f"  ✅ {got['n']}일 ({got['range'][0]} ~ {got['range'][1]})")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 실패: {exc}")
    print()
    print(format_status())
    return 0


def format_status() -> str:
    """지금 보유량과 **국면 포함 여부**."""
    lines = ["📦 소급 수집 현황", ""]
    root = _cache_root()
    items = [("KOSPI 지수", "indices", "market_index_KOSPI_*.json"),
             ("VKOSPI", "indices", "market_index_VKOSPI_*.json"),
             ("VIX", "vix", "vix_*.json"),
             ("원/달러", "fx", "usdkrw_*.json"),
             ("외국인 순매수", "flow", "foreign_net_*.json")]
    kospi_series = None
    for label, subdir, pattern in items:
        files = sorted((root / subdir).glob(pattern))
        if not files:
            lines.append(f"  {label:14s} 없음")
            continue
        try:
            payload = json.loads(files[-1].read_text(encoding="utf-8"))
            series = payload.get("series") or {}
            dates = [str(d) for d in series.get("date") or []]
            closes = [float(c) for c in series.get("close") or []]
        except (OSError, ValueError, TypeError):
            lines.append(f"  {label:14s} 읽기 실패")
            continue
        lines.append(f"  {label:14s} {len(dates):4d}일  {dates[0]} ~ {dates[-1]}")
        if label == "KOSPI 지수":
            kospi_series = (dates, closes)

    lines.append("")
    if kospi_series:
        span = regime_span(*kospi_series)
        lines.append("■ 정답 구간에 국면 전환이 있는가")
        if not span["n"]:
            lines.append("  200일선 판정 가능 구간이 없습니다(표본 부족).")
        else:
            lines.append(f"  판정 가능 {span['n']}일 · 200일선 위 {span['above']}일 · "
                         f"아래 {span['below']}일({span['below_pct']}%) · 전환 {span['flips']}회")
            if span["has_both"] and span["below_pct"] and span["below_pct"] >= 10:
                lines.append("  ✅ 두 국면이 모두 들어 있습니다 — 판정 가능한 표본입니다.")
            else:
                lines.append("  ⚠️ 한 국면에 쏠려 있습니다. **기다릴 게 아니라 더 소급하면 됩니다.**")
                lines.append("     예: `collect_history.py --kospi 2000` (약 8년)")
    lines.append("")
    lines.append("■ 거시 국면 이력 (탭 C)")
    try:
        import json as _json

        from storage_paths import PATHS

        pf = PATHS.private_state_dir / "phase_history.json"
        if pf.exists():
            rows = _json.loads(pf.read_text(encoding="utf-8"))
            import phase_history as ph

            d = ph.describe(rows)
            lines.append(f"  {d['n']}개월 · 국면 {d['distinct']}종 · 전환 {d['flips']}회"
                         + ("  ✅ 측정 가능" if d["usable"] else "  ⚠️ 아직 부족"))
        else:
            lines.append("  없음 — `phase_history.py --months 120 --save`로 소급 재구성")
    except Exception:  # noqa: BLE001
        lines.append("  확인 실패")

    lines.append("")
    lines.append("소급 불가(시계를 기다려야 하는 것): 봇 실제 거래 기록 · mode 판정 · 사용자 행동")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(_cli())
