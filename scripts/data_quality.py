"""data_quality.py — 성과 집계에서 제외할 거래 (v1, 순수 + 얇은 경계)

**왜 지우지 않고 제외하는가**: 잘못된 기록을 DB에서 없애면 나중에 "왜 숫자가
달라졌는지"를 아무도 되짚을 수 없다. 원본은 남기고, 집계에서만 뺀다.
무엇을 왜 뺐는지는 `excluded_trades.json`에 사유와 함께 남는다.

현재 등록된 사유:
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
from typing import Iterable, Optional

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
