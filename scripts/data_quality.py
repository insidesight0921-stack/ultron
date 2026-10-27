"""data_quality.py — 성과 집계에서 제외할 거래 (v1, 순수 + 얇은 경계)

**왜 지우지 않고 제외하는가**: 잘못된 기록을 DB에서 없애면 나중에 "왜 숫자가
달라졌는지"를 아무도 되짚을 수 없다. 원본은 남기고, 집계에서만 뺀다.
무엇을 왜 뺐는지는 `excluded_trades.json`에 사유와 함께 남는다.

현재 등록된 사유:
  - `fill_out_of_range` — 체결가가 **그날 실제 거래된 저가~고가 밖**인 건(2026-10-26).
    08-27 안전장치(price_sanity 차단·장외 대기 큐) 이전에는 장외·개장직후 매수가
    전 거래일 종가에 슬리피지를 얹은 값으로 기록됐다. 원 단위 일치가 아니라서
    `stale_price`에 안 걸렸고 `fill_assumption`(표시만)으로 남았는데, 07-28 LG이노텍은
    매수가 614,921원 · 그날 고가 575,000원 — **시장에서 불가능한 체결**이다.
    안전장치 이전 건은 제외, 이후 건은 표시만 한다(사용자 결정, 2026-10-26).
    판정은 수익·손실을 가리지 않는다 — +20% 두 건도 같이 빠졌다.
  - `stale_price` — 자동 매수가 오래된 종가로 진입 기록을 남긴 건.
    2026-06-08 10:58 배치 7종목의 진입가가 **4거래일 전(06-01) 종가와 원 단위까지
    정확히 일치**했다. 22분 뒤 모니터가 진짜 시세를 읽자 −7% 손절선이 즉시 발동해
    8종목이 전량 청산됐다(−7.9% ~ −25.6%, 합계 −6,786,077원). 시장이 낸 손실이 아니라
    가격 오류가 만든 손실이므로 전략 평가에서 빼야 한다.

제외는 **라운드트립 단위**로 한다. 매수 행만 빼면 FIFO가 뒤의 매수와 잘못 짝지어져
오히려 더 큰 왜곡이 생긴다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("data_quality")

STATE_NAME = "excluded_trades.json"


# ─── 매칭 (순수) ─────────────────────────────────────


def rule_key(rule: dict) -> tuple:
    """제외 규칙 → 비교 키. 분 단위까지만 본다(초는 기록마다 흔들린다)."""
    return (str(rule.get("slot") or ""), str(rule.get("ticker") or ""),
            str(rule.get("buy_at") or "")[:16])


def roundtrip_key(rt: dict) -> tuple:
    return (str(rt.get("slot") or ""), str(rt.get("ticker") or ""),
            str(rt.get("buy_at") or "")[:16])


def matches(rt: dict, rules: Iterable[dict]) -> Optional[dict]:
    """이 라운드트립에 걸리는 규칙, 없으면 None(순수). 제외/라벨 여부는 보지 않는다."""
    key = roundtrip_key(rt)
    for rule in rules:
        if rule_key(rule) == key:
            return rule
    return None


def is_exclusion(rule: dict) -> bool:
    """`exclude: false`면 라벨만 붙이고 집계에는 남긴다.

    모든 의심 기록을 빼면 표본이 사라지고, 손실만 골라 빼면 결과가 좋아지는 쪽으로
    데이터를 고르는 것이 된다. 그래서 '뺄 것'과 '표시만 할 것'을 규칙에서 나눈다.
    """
    return rule.get("exclude", True) is not False


def split(rts: Iterable[dict], rules: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """(집계에 쓸 것, 제외된 것). 제외분에는 `excluded_reason`을 붙여 돌려준다."""
    rules = list(rules)
    kept, dropped = [], []
    for rt in rts:
        rule = matches(rt, rules) if rules else None
        if rule is None:
            kept.append(rt)
            continue
        item = dict(rt)
        item["flag_reason"] = rule.get("reason") or rule.get("kind") or "표시"
        item["flag_kind"] = rule.get("kind") or "unknown"
        if is_exclusion(rule):
            item["excluded_reason"] = item["flag_reason"]
            item["excluded_kind"] = item["flag_kind"]
            dropped.append(item)
        else:
            kept.append(item)        # 라벨만 — 집계에는 남는다
    return kept, dropped


def flagged(rts: Iterable[dict]) -> list[dict]:
    """집계에 남아 있지만 표시가 붙은 것들(순수)."""
    return [r for r in rts if r.get("flag_kind") and not r.get("excluded_kind")]


def summary(dropped: list[dict]) -> dict:
    """제외분 요약 — 화면이 '무엇을 뺐는지'를 말할 수 있어야 한다."""
    return {
        "n": len(dropped),
        "pnl": round(sum(float(r.get("pnl") or 0) for r in dropped)),
        "kinds": sorted({r.get("excluded_kind", "unknown") for r in dropped}),
        "rows": [
            {"slot": r.get("slot"), "ticker": r.get("ticker"), "name": r.get("name"),
             "buy_at": r.get("buy_at"), "pnl": round(float(r.get("pnl") or 0)),
             "ret": round(float(r.get("ret") or 0) * 100, 1),
             "reason": r.get("excluded_reason")}
            for r in dropped
        ],
    }


def format_summary(s: dict) -> str:
    if not s["n"]:
        return ""
    lines = [f"🧹 성과 집계 제외 {s['n']}건 ({s['pnl']:,}원)"]
    for r in s["rows"][:10]:
        lines.append(f"• {r['name'] or r['ticker']} {r['buy_at'][:16]} "
                     f"{r['ret']:+.1f}% {r['pnl']:,}원 — {r['reason']}")
    lines.append("")
    lines.append("_기록은 DB에 그대로 남아 있습니다. 집계에서만 뺐습니다._")
    return "\n".join(lines)


# ─── 체결가 범위 검사 (순수) ─────────────────────────

# price_sanity 차단·장외 신호 대기 큐가 들어간 날. 이전 범위 밖 체결은 제외, 이후는 표시.
SAFEGUARD_SINCE = "2026-08-27"
# 일봉 저가·고가와 견줄 때의 여유. 슬리피지(0.15%)·호가 단위를 덮되 전일 종가 재사용
# (하루 수 % 차이)는 잡는 폭.
FILL_TOL = 0.005
FILL_KIND = "fill_out_of_range"


def fill_out_of_range(rt: dict, bar_for: Callable[[str, str], Optional[tuple]],
                      tol: float = FILL_TOL) -> Optional[list[str]]:
    """체결가가 그날 [저가, 고가] 밖이면 사유 목록, 안이면 [], 일봉이 없으면 None(순수).

    `bar_for(ticker, "YYYYMMDD") -> (low, high) | None`. 모르는 날을 「정상」으로
    세지 않는다 — None은 검사 불가다.
    """
    out, known = [], False
    for side, at_key, px_key in (("매수", "buy_at", "buy_price"), ("매도", "sell_at", "sell_price")):
        px, at = rt.get(px_key), str(rt.get(at_key) or "")
        if px is None or len(at) < 10:
            continue
        bar = bar_for(str(rt.get("ticker") or ""), at[:10].replace("-", ""))
        if not bar:
            continue
        known = True
        low, high = bar
        if not (low * (1 - tol) <= float(px) <= high * (1 + tol)):
            out.append(f"{side}가 {float(px):,.0f}원 — {at[:10]} 범위 {low:,.0f}~{high:,.0f}원 밖")
    return out if known else None


def fill_rules(rts: Iterable[dict], bar_for, *, detected_at: str,
               since: str = SAFEGUARD_SINCE, tol: float = FILL_TOL) -> list[dict]:
    """범위 밖 체결 → 규칙 목록(순수). 안전장치 이전 매수만 제외, 이후는 표시."""
    rules, seen = [], set()
    for rt in rts:
        reasons = fill_out_of_range(rt, bar_for, tol)
        if not reasons:
            continue
        key = roundtrip_key(rt)
        if key in seen:                    # 같은 매수를 나눠 판 라운드트립 — 규칙은 하나
            continue
        seen.add(key)
        before = str(rt.get("buy_at") or "")[:10] < since
        rules.append({
            "slot": key[0], "ticker": key[1], "name": rt.get("name"),
            "buy_at": rt.get("buy_at"), "kind": FILL_KIND, "exclude": before,
            "reason": ("; ".join(reasons) + (" — 안전장치 이전, 불가능한 체결" if before
                                             else " — 안전장치 이후, 표시만")),
            "detected_at": detected_at, "pnl_at_detection": round(float(rt.get("pnl") or 0)),
        })
    return rules


def merge_rules(existing: list[dict], new: list[dict]) -> tuple[list[dict], list[str]]:
    """새 규칙을 기존 목록에 합친다(순수). (합친 목록, 바뀐 것 설명).

    - 이미 **제외** 중인 키(`stale_price` 등)는 건드리지 않는다.
    - **표시만** 하던 키는 새 규칙으로 바꾸고, 이전 판단을 `supersedes`에 남긴다 —
      규칙 하나가 키 하나를 맡아야 `matches`가 어느 것을 쓸지 흔들리지 않는다.
    - 같은 종류가 이미 있으면 그대로(다시 돌려도 같은 결과).
    """
    out = [dict(r) for r in existing]
    index = {rule_key(r): i for i, r in enumerate(out)}
    changes = []
    for rule in new:
        k = rule_key(rule)
        i = index.get(k)
        if i is None:
            out.append(dict(rule))
            index[k] = len(out) - 1
            changes.append(f"추가 {k[0]} {rule.get('name') or k[1]} {k[2]}")
            continue
        old = out[i]
        if old.get("kind") == rule.get("kind") or is_exclusion(old):
            continue
        out[i] = dict(rule, supersedes={f: old.get(f) for f in
                                        ("kind", "exclude", "reason", "detected_at")})
        changes.append(f"교체 {k[0]} {rule.get('name') or k[1]} {k[2]} "
                       f"({old.get('kind')}·표시만 → {rule['kind']}·"
                       f"{'제외' if rule['exclude'] else '표시만'})")
    return out, changes


# ─── 경계 (I-O) ──────────────────────────────────────


def default_path() -> Path:
    try:
        from storage_paths import PATHS
        return Path(PATHS.private_state_file(STATE_NAME))
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "data" / "private" / "state" / STATE_NAME


def load_rules(path=None) -> list[dict]:
    """제외 규칙 목록. 파일이 없거나 깨졌으면 빈 목록 — 제외를 못 해도 집계는 돌아야 한다."""
    p = Path(path) if path else default_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def save_rules(rules: list[dict], path=None) -> None:
    p = Path(path) if path else default_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".tmp_{p.name}"
        tmp.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        log.warning("제외 규칙 저장 실패: %s", e)


def apply(rts: Iterable[dict], path=None) -> tuple[list[dict], list[dict]]:
    """운영 규칙 파일을 읽어 적용."""
    return split(rts, load_rules(path))


def cached_bar_lookup(cache_dir=None) -> Callable[[str, str], Optional[tuple]]:
    """종목 일봉 캐시(가장 최근 파일) → bar_for(ticker, day). 얇은 I/O."""
    if cache_dir is None:
        from storage_paths import PATHS
        cache_dir = Path(PATHS.shareable_root) / "cache" / "ohlcv"
    cache_dir = Path(cache_dir)
    memo: dict[str, dict] = {}

    def bar_for(ticker: str, day: str):
        if ticker not in memo:
            files = sorted(cache_dir.glob(f"{ticker}_*.json"))
            bars = {}
            if files:
                try:
                    d = json.loads(files[-1].read_text(encoding="utf-8"))
                    for dt, lo, hi in zip(d["date"], d["low"], d["high"]):
                        bars[str(dt).replace("-", "")[:8]] = (float(lo), float(hi))
                except (OSError, ValueError, KeyError, TypeError):
                    bars = {}
            memo[ticker] = bars
        return memo[ticker].get(day)

    return bar_for


def _cli() -> int:
    import argparse
    from datetime import date

    ap = argparse.ArgumentParser(description="성과 집계 제외 규칙 — 체결가 범위 검사")
    ap.add_argument("--scan-fills", action="store_true",
                    help="체결가가 그날 저가~고가 밖인 라운드트립을 찾는다(기본: 미리보기)")
    ap.add_argument("--apply", action="store_true", help="찾은 규칙을 규칙 파일에 반영한다")
    args = ap.parse_args()
    if not args.scan_fills:
        ap.print_help()
        return 0

    import paper_db
    import trade_analytics as ta

    # **규칙을 적용하기 전** 라운드트립을 본다. 적용 후를 보면 이미 뺀 건이 사라져
    # 두 번째 실행부터 「제외 0건」으로 찍힌다 — 결과는 같아도 보고가 흔들린다.
    rts = ta.compute_roundtrips(paper_db.list_trades(limit=100000))
    bar_for = cached_bar_lookup()
    unknown = sum(1 for rt in rts if fill_out_of_range(rt, bar_for) is None)
    new = fill_rules(rts, bar_for, detected_at=date.today().isoformat())
    merged, changes = merge_rules(load_rules(), new)
    print(f"라운드트립 {len(rts)}건 · 범위 밖 {len(new)}건(매수 기준 키) · 일봉 없어 검사 불가 {unknown}건")
    print(f"안전장치({SAFEGUARD_SINCE}) 이전 → 제외 {sum(r['exclude'] for r in new)}건 · "
          f"이후 → 표시만 {sum(not r['exclude'] for r in new)}건")
    for c in changes:
        print("  " + c)
    if not changes:
        print("  바뀔 것 없음")
    if args.apply and changes:
        save_rules(merged)
        print(f"반영 — {default_path().name} ({len(merged)}개 규칙)")
    elif changes:
        print("미리보기입니다. 반영하려면 --apply")
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(_cli())
