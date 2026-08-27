"""price_sanity.py — 진입가 스테일 탐지 (v1, 순수 + 얇은 경계)

**막으려는 사고**: 2026-06-08 10:58 자동 매수에서 7종목의 진입가가
**4거래일 전(06-01) 종가와 원 단위까지 정확히 일치**했다. 오래된 가격으로
진입 기록이 남았고, 22분 뒤 모니터가 진짜 시세를 읽자 −7% 손절선이 즉시 발동해
8종목이 전량 청산됐다(−7.9% ~ −25.6%, 합계 −6,786,077원). 전체 누적 손익이
−6,346,942원이었으니, 이 한 배치가 기록 전체의 부호를 뒤집고 있었다.

**탐지 원리**: 한 종목의 가격이 우연히 과거 어느 종가와 같을 수는 있다. 하지만
**한 배치의 여러 종목이 모두 같은 과거 날짜의 종가와 원 단위까지 일치**하는 일은
우연히 일어나지 않는다. 그래서 개별 종목이 아니라 **배치의 날짜 정렬**을 본다.
오탐이 거의 없고, 실제 사고를 정확히 집는다.

**대응**: 의심되면 그 배치의 매수를 **막는다**(fail-closed). 진입가가 틀리면
그 뒤의 손절·성과·회고가 전부 오염되므로, 사는 것을 미루는 쪽이 싸다.
매도는 막지 않는다 — 보유를 정리할 길까지 막으면 더 위험하다.

**하지 않는 것**: 개별 종목의 괴리율(예: 최근 종가 대비 ±15%)로는 막지 않는다.
KRX 가격제한폭이 ±30%라 상한가·하한가가 정상적으로 그 범위를 넘어선다.
괴리는 경고로만 남긴다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("price_sanity")

# 원 단위 일치로 볼 허용 오차. 종가는 정수 호가라 0.5원이면 충분하다.
EXACT_EPS = 0.5
# 배치에서 몇 종목이 같은 과거 날짜에 걸리면 스테일로 볼 것인가.
MIN_BATCH_HITS = int(os.getenv("PRICE_STALE_MIN_HITS", "2"))
# 개별 종목 괴리 경고선(%). 차단하지 않는다.
DEVIATION_WARN_PCT = float(os.getenv("PRICE_DEVIATION_WARN_PCT", "15"))


# ─── 탐지 (순수) ─────────────────────────────────────


def exact_match_dates(price: float, series: list[tuple[str, float]],
                      *, today: str, eps: float = EXACT_EPS) -> list[str]:
    """가격과 원 단위까지 같은 **과거** 거래일들. 당일은 제외한다(정상이므로)."""
    if not price or price <= 0:
        return []
    return [d for d, close in (series or [])
            if d < today and close is not None and abs(float(close) - price) <= eps]


def deviation_pct(price: float, latest_close: Optional[float]) -> Optional[float]:
    if not price or price <= 0 or not latest_close or latest_close <= 0:
        return None
    return round((price / latest_close - 1) * 100, 2)


def inspect_batch(items: Iterable[dict], series_by_ticker: dict, today: str,
                  *, min_hits: int = MIN_BATCH_HITS,
                  warn_pct: float = DEVIATION_WARN_PCT) -> dict:
    """배치 진단(순수).

    items: [{ticker, name, price}]
    series_by_ticker: {ticker: [(YYYYMMDD, close), ...]}
    today: YYYYMMDD

    반환: {stale: bool, stale_date, hits: [...], warnings: [...], checked, reason}
    """
    items = [dict(i) for i in items]
    by_date: dict[str, list[dict]] = {}
    warnings: list[dict] = []
    checked = 0

    for item in items:
        series = series_by_ticker.get(str(item.get("ticker")))
        if not series:
            continue
        checked += 1
        price = float(item.get("price") or 0)
        for d in exact_match_dates(price, series, today=today):
            by_date.setdefault(d, []).append(item)

        latest = next((c for dd, c in reversed(series) if dd <= today), None)
        dev = deviation_pct(price, latest)
        if dev is not None and abs(dev) > warn_pct:
            warnings.append({"ticker": item.get("ticker"), "name": item.get("name"),
                             "price": price, "close": latest, "deviation": dev})

    stale_date, hits = None, []
    for d, group in sorted(by_date.items()):
        if len(group) >= min_hits and len(group) > len(hits):
            stale_date, hits = d, group

    if stale_date:
        reason = (f"{len(hits)}종목의 진입가가 {stale_date} 종가와 원 단위까지 "
                  f"일치합니다 — 오래된 가격으로 보입니다")
    elif checked == 0:
        reason = "비교할 일봉이 없어 판정하지 않음"
    else:
        reason = f"{checked}종목 확인 — 스테일 징후 없음"

    return {"stale": bool(stale_date), "stale_date": stale_date,
            "hits": [{"ticker": h.get("ticker"), "name": h.get("name"),
                      "price": float(h.get("price") or 0)} for h in hits],
            "warnings": warnings, "checked": checked, "reason": reason}


def format_block(verdict: dict) -> str:
    """차단 알림(순수). 스테일이 아니면 빈 문자열."""
    if not verdict.get("stale"):
        return ""
    lines = ["🛑 *진입가 이상 — 자동 매수를 막았습니다*", "", verdict["reason"], ""]
    for h in verdict["hits"]:
        lines.append(f"• {h['name'] or h['ticker']}: {h['price']:,.0f}원")
    lines += ["",
              "_틀린 진입가로 사면 그 뒤의 손절·성과·회고가 전부 오염됩니다._",
              "_시세 조회를 확인한 뒤 다시 시도하세요. 보유 종목 매도는 막지 않습니다._"]
    return "\n".join(lines)


def format_warnings(verdict: dict) -> str:
    """괴리 경고(순수). 차단하지는 않는다."""
    ws = verdict.get("warnings") or []
    if not ws:
        return ""
    lines = [f"⚠️ 최근 종가와 {DEVIATION_WARN_PCT:.0f}% 넘게 차이 나는 진입가 {len(ws)}건"]
    for w in ws:
        lines.append(f"• {w['name'] or w['ticker']}: {w['price']:,.0f}원 "
                     f"(종가 {w['close']:,.0f}원, {w['deviation']:+.1f}%)")
    lines.append("_상한가·하한가면 정상입니다. 차단하지 않습니다._")
    return "\n".join(lines)


# ─── 경계 (I-O) ──────────────────────────────────────


def _cache_root() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.shareable_cache_dir)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "shareable" / "cache"


def trading_calendar() -> list[str]:
    """KRX 거래일 달력 — KOSPI 지수 캐시의 날짜열을 그대로 쓴다."""
    try:
        files = sorted((_cache_root() / "indices").glob("market_index_KOSPI_*.json"))
        if not files:
            return []
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
        return list(payload["series"]["date"])
    except Exception as e:  # noqa: BLE001
        log.debug("거래일 달력 로드 실패: %s", e)
        return []


def load_series(ticker: str, calendar: list[str]) -> list[tuple[str, float]]:
    """종목 일봉 캐시 → [(YYYYMMDD, 종가)].

    캐시 파일에는 종가 배열만 있고 날짜가 없다. 파일명의 기준일이 마지막 값이라는
    전제로 거래일 달력을 거꾸로 붙인다. 길이가 달력을 넘으면 정렬을 신뢰할 수 없으므로
    빈 목록을 돌려준다 — **어긋난 정렬로 판정하느니 판정하지 않는 편이 낫다.**
    """
    d = _cache_root() / "ohlcv"
    try:
        files = sorted(d.glob(f"{ticker}_*.json"))
        if not files or not calendar:
            return []
        path = files[-1]
        as_of = path.stem.split("_")[-1]
        if as_of not in calendar:
            return []
        closes = json.loads(path.read_text(encoding="utf-8")).get("close") or []
        end = calendar.index(as_of)
        if not closes or end + 1 < len(closes):
            return []
        return list(zip(calendar[end - len(closes) + 1: end + 1], closes))
    except Exception as e:  # noqa: BLE001
        log.debug("일봉 캐시 로드 실패 %s: %s", ticker, e)
        return []


def check_batch(items: list[dict], today: str, **kw) -> dict:
    """운영 캐시를 읽어 배치를 진단한다."""
    calendar = trading_calendar()
    series = {}
    for item in items:
        t = str(item.get("ticker") or "")
        if t and t not in series:
            series[t] = load_series(t, calendar)
    return inspect_batch(items, series, today, **kw)


def _cli() -> int:
    """기존 거래 기록을 소급 점검한다."""
    import argparse
    import sqlite3
    from collections import defaultdict

    ap = argparse.ArgumentParser(description="진입가 스테일 소급 점검")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    import paper_db
    con = sqlite3.connect(args.db or paper_db.DEFAULT_DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select s.name slot, t.ticker, t.name, t.price, t.executed_at "
        "from trades t join slots s on s.id=t.slot_id where t.side='buy' "
        "order by t.executed_at")]

    calendar = trading_calendar()
    series: dict = {}
    batches = defaultdict(list)
    for r in rows:
        batches[(r["slot"], r["executed_at"][:16])].append(r)

    found = 0
    for (slot, when), items in sorted(batches.items()):
        if len(items) < MIN_BATCH_HITS:
            continue
        for it in items:
            series.setdefault(it["ticker"], load_series(it["ticker"], calendar))
        v = inspect_batch(items, series, when[:10].replace("-", ""))
        if v["stale"]:
            found += 1
            print(f"\n🛑 {slot} {when} — {v['reason']}")
            for h in v["hits"]:
                print(f"   {h['name']} {h['price']:,.0f}원")
    print(f"\n의심 배치 {found}건 / 전체 {len(batches)}배치")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
