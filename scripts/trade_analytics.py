"""trade_analytics.py — paper 거래 심화 분석 (트레이드 단위, v1.2)

paper_analytics(슬롯 단위 성과)를 보완하는 **트레이드 단위** 분석:
  1) 매매 습관 — 손익비(PF)·평균이익/손실·손절vs익절 빈도·보유기간
  2) 종목별 실현손익 — 베스트/워스트 랭킹(FIFO)
  3) 기간별 추세 — 월별 실현손익 + (선택) perf_history 스냅샷 추세
  4) LLM 자연어 총평 — 위 수치를 로컬 Gemma가 읽고 한국어 진단(주입 가능)
  + 정밀 진단(v1.1) — 손절 실행 품질(슬리피지)·휩쏘(손절 후 재매수)·손실 집중도
  + 국면·섹터 상관(v1.2) — correlate_pnl_by(label_fn 주입) 라벨별 손익·손실 집중

설계:
  - 순수 함수(거래 리스트만) → hermetic 테스트. deep_report()만 paper_db/LLM 호출(지연·주입).
  - LLM 실패해도 수치 분석은 유지(graceful).
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from storage_paths import PATHS

log = logging.getLogger("trade_analytics")

OLLAMA_URL = "http://127.0.0.1:11434"
LLM_MODEL = "gemma4:31b"
STOP_LOSS_PCT = -7.0  # 시스템 손절선(참고값) — 슬리피지 진단 기준


# ─── FIFO 라운드트립 (순수) ──────────────────────────


def _classify_reason(note: str) -> str:
    note = note or ""
    if "손절" in note:
        return "손절"
    if "익절" in note:
        return "익절"
    if "리밸" in note or "청산" in note:
        return "리밸런싱"
    return "수동/기타"


def _parse_dt(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def compute_roundtrips(trades: list[dict]) -> list[dict]:
    """매수/매도 거래 → FIFO 완결 라운드트립 리스트(순수).

    trades: {slot_name, ticker, name, side, quantity, price, fees, notes, executed_at}.
    반환 각 항목: slot, ticker, name, pnl, cost, ret, buy_at, sell_at, hold_days, reason.
    """
    ordered = sorted(trades, key=lambda t: (t.get("executed_at") or "", t.get("id") or 0))
    queues: dict[tuple, deque] = defaultdict(deque)
    out: list[dict] = []

    for t in ordered:
        key = (t.get("slot_name") or t.get("slot") or "?", t.get("ticker"))
        qty = int(t.get("quantity") or 0)
        price = float(t.get("price") or 0)
        fees = float(t.get("fees") or 0)
        if qty <= 0:
            continue
        if t.get("side") == "buy":
            queues[key].append([qty, price, fees / qty, t.get("executed_at"),
                                t.get("name"), t.get("notes")])
        else:
            remaining, cost, matched = qty, 0.0, 0
            buy_at, name, buy_notes = None, t.get("name"), None
            while remaining > 0 and queues[key]:
                bq, bp, bfpu, bdt, bname, bnotes = queues[key][0]
                take = min(remaining, bq)
                cost += take * bp + take * bfpu
                remaining -= take
                matched += take
                buy_at = buy_at or bdt
                name = name or bname
                buy_notes = buy_notes or bnotes
                if take >= bq:
                    queues[key].popleft()
                else:
                    queues[key][0][0] = bq - take
            if matched > 0:
                sell_rev = matched * price - fees * (matched / qty)
                pnl = sell_rev - cost
                bd, sd = _parse_dt(buy_at), _parse_dt(t.get("executed_at"))
                hold = (sd - bd).days if bd and sd else None
                out.append({
                    "slot": key[0], "ticker": key[1], "name": name or key[1],
                    "pnl": pnl, "cost": cost, "qty": matched,
                    "ret": (pnl / cost if cost else 0.0),
                    "buy_at": buy_at, "sell_at": t.get("executed_at"),
                    "buy_notes": buy_notes,
                    "hold_days": hold, "reason": _classify_reason(t.get("notes")),
                })
    return out


def roundtrips_for_analysis(trades: list[dict], rules_path=None) -> tuple[list[dict], list[dict]]:
    """분석용 라운드트립 — 데이터 품질 규칙을 적용한 (집계 대상, 제외분).

    `compute_roundtrips`는 기록 그대로를 돌려주는 순수 함수로 남긴다. 품질 판단이
    섞이면 "원본이 무엇이었나"를 볼 수 없게 된다. 성과·전략 분석은 이쪽을 쓴다.
    슬롯 일일 한도처럼 **오늘의 실제 위험**을 보는 곳은 원본을 그대로 쓴다.
    """
    rts = compute_roundtrips(trades)
    try:
        import data_quality
        return data_quality.apply(rts, rules_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("데이터 품질 규칙 적용 실패 — 원본으로 진행: %s", exc)
        return rts, []


# ─── 1) 매매 습관 (순수) ─────────────────────────────


def habit_stats(rts: list[dict]) -> dict:
    n = len(rts)
    wins = [r for r in rts if r["pnl"] > 0]
    losses = [r for r in rts if r["pnl"] <= 0]
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = sum(r["pnl"] for r in losses)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    reasons: dict[str, int] = defaultdict(int)
    for r in rts:
        reasons[r["reason"]] += 1
    holds = [r["hold_days"] for r in rts if r["hold_days"] is not None]
    return {
        "n_closed": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "total_pnl": round(gross_win + gross_loss),
        "avg_win": round(avg_win), "avg_loss": round(avg_loss),
        "profit_factor": (round(gross_win / abs(gross_loss), 2)
                          if gross_loss else (None if not wins else float("inf"))),
        "payoff_ratio": (round(avg_win / abs(avg_loss), 2) if avg_loss else None),
        "reason_counts": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
    }


# ─── 2) 종목별 손익 (순수) ───────────────────────────


def by_ticker(rts: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rts:
        a = agg.setdefault(r["name"], {"name": r["name"], "ticker": r["ticker"],
                                       "pnl": 0.0, "n": 0})
        a["pnl"] += r["pnl"]
        a["n"] += 1
    rows = [{"name": v["name"], "ticker": v["ticker"],
             "pnl": round(v["pnl"]), "n": v["n"]} for v in agg.values()]
    return sorted(rows, key=lambda x: x["pnl"], reverse=True)


# ─── 3) 기간별 추세 (순수) ───────────────────────────


def monthly_pnl(rts: list[dict]) -> dict[str, int]:
    m: dict[str, float] = defaultdict(float)
    for r in rts:
        key = (r.get("sell_at") or "")[:7]
        if key:
            m[key] += r["pnl"]
    return {k: round(m[k]) for k in sorted(m)}


# ─── 정밀 진단: 손절 품질 / 휩쏘 / 손실 집중 (순수) ──


def stop_loss_diagnosis(rts: list[dict], threshold: float = STOP_LOSS_PCT) -> dict:
    """손절(reason=='손절') 청산의 수익률 분포·평균·손절선 대비 슬리피지.

    slippage < 0 → 손절선보다 깊게(늦게) 실행됨(지연/갭). 손절선 타이트 여부의 실증.
    """
    stops = [r for r in rts if r["reason"] == "손절"]
    if not stops:
        return {"n": 0}
    rets = [r["ret"] * 100 for r in stops]
    avg = sum(rets) / len(rets)
    return {
        "n": len(stops),
        "avg_ret": round(avg, 1),
        "threshold": threshold,
        "slippage": round(avg - threshold, 1),
        "deeper_than_stop": sum(1 for x in rets if x < threshold),
        "much_deeper": sum(1 for x in rets if x < threshold - 5),
    }


EXIT_FIX_DEPLOYED = "2026-07-09"  # v3.44 손절 실행 지연 수정 배포일 — 전/후 슬리피지 비교 기준


def slippage_comparison(rts: list[dict], cutoff: str = EXIT_FIX_DEPLOYED,
                        threshold: float = STOP_LOSS_PCT) -> dict:
    """손절 슬리피지를 수정 배포 전/후로 나눠 비교(순수). 손절선 조정 판단 근거."""
    before = [r for r in rts if (r.get("sell_at") or "") < cutoff]
    after = [r for r in rts if (r.get("sell_at") or "") >= cutoff]
    return {"cutoff": cutoff,
            "before": stop_loss_diagnosis(before, threshold),
            "after": stop_loss_diagnosis(after, threshold)}


def whipsaw(trades: list[dict], rts: list[dict], days: int = 14) -> list[dict]:
    """손절 후 days일 내 같은 종목 재매수(휩쏘) 탐지. [{name, days}]."""
    buys: dict[tuple, list] = defaultdict(list)
    for t in trades:
        if t.get("side") == "buy":
            key = (t.get("slot_name") or t.get("slot") or "?", t.get("ticker"))
            buys[key].append(_parse_dt(t.get("executed_at")))
    out: list[dict] = []
    for r in rts:
        if r["reason"] != "손절":
            continue
        key = (r["slot"], r["ticker"])
        sdt = _parse_dt(r["sell_at"])
        for bd in buys.get(key, []):
            if bd and sdt and 0 < (bd - sdt).days <= days:
                out.append({"name": r["name"], "days": (bd - sdt).days})
                break
    return out


def loss_concentration(rts: list[dict], top: int = 3) -> dict:
    """실현손실의 종목 집중도 — 워스트 N 점유율."""
    losses: dict[str, float] = defaultdict(float)
    for r in rts:
        if r["pnl"] < 0:
            losses[r["name"]] += r["pnl"]
    if not losses:
        return {"n_losers": 0, "total_loss": 0, "top": [], "top_share_pct": None}
    total = sum(losses.values())
    srt = sorted(losses.items(), key=lambda kv: kv[1])[:top]
    top_sum = sum(v for _, v in srt)
    return {
        "n_losers": len(losses), "total_loss": round(total),
        "top": [{"name": n, "pnl": round(v)} for n, v in srt],
        "top_share_pct": round(top_sum / total * 100) if total else None,
    }


# ─── 국면·섹터 상관 (순수, label_fn 주입) ─────────────

# 수동 섹터 맵 — paper.db 실거래 14종목 기준(2026-07). 신규 종목은 여기에 추가.
SECTOR_BY_TICKER = {
    "005930": "반도체",        # 삼성전자
    "000660": "반도체",        # SK하이닉스
    "042700": "반도체",        # 한미반도체
    "009150": "IT부품",        # 삼성전기
    "011070": "IT부품",        # LG이노텍
    "402340": "지주/투자",     # SK스퀘어
    "006800": "증권",          # 미래에셋증권
    "047040": "건설",          # 대우건설
    "010120": "전력설비",      # LS ELECTRIC
    "001440": "전력설비",      # 대한전선
    "298040": "전력설비",      # 효성중공업
    "010060": "화학",          # OCI홀딩스
    "278470": "화장품",        # 에이피알
    "307950": "IT서비스",      # 현대오토에버
}

# 월별 거시 국면 — 수동 근사 라벨(quant_bot consensus_phase 기준으로 갱신).
# ⚠️ 근사값: 과거 시점 재계산이 아닌 수동 라벨. 새 달은 여기에 추가.
PHASE_BY_MONTH = {
    "2026-05": "Expansion",
    "2026-06": "Slowdown",
    "2026-07": "Slowdown",
}

UNLABELED = "미분류"

# 실측 국면 캐시 — quant_bot snapshot 실행 시마다 그 달의 consensus_phase 기록(자동 축적).
# merged_phase_map()에서 수동 근사(PHASE_BY_MONTH)보다 우선 적용.
_PHASE_CACHE_PATH = PATHS.private_state_file("phase_by_month.json")


def load_phase_months(path=None) -> dict:
    """실측 국면 캐시 {YYYY-MM: phase}. 실패 시 {}."""
    p = Path(path) if path else _PHASE_CACHE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_phase_month(phase: str, month: Optional[str] = None,
                       path=None) -> None:
    """이번 달(기본) 국면을 캐시에 기록 — quant snapshot 성공 시 호출."""
    if not phase:
        return
    p = Path(path) if path else _PHASE_CACHE_PATH
    m = month or datetime.now().strftime("%Y-%m")
    try:
        cache = load_phase_months(p)
        cache[m] = phase
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"국면 캐시 기록 실패: {e}")


def merged_phase_map(path=None) -> dict:
    """수동 근사 + 실측 캐시 병합(실측 우선)."""
    return {**PHASE_BY_MONTH, **load_phase_months(path)}


def sector_label(rt: dict, mapping: Optional[dict] = None) -> str:
    """라운드트립 → 섹터 라벨(수동 맵, 미등록은 UNLABELED)."""
    m = SECTOR_BY_TICKER if mapping is None else mapping
    return m.get(rt.get("ticker"), UNLABELED)


def phase_label(rt: dict, mapping: Optional[dict] = None) -> str:
    """라운드트립 → 실현월(sell_at) 기준 거시 국면 라벨(수동 근사 맵)."""
    m = PHASE_BY_MONTH if mapping is None else mapping
    month = (rt.get("sell_at") or "")[:7]
    return m.get(month, UNLABELED)


def correlate_pnl_by(rts: list[dict],
                     label_fn: Callable[[dict], str]) -> dict[str, dict]:
    """라벨별 실현손익 집계(순수). label_fn(rt)->라벨 을 주입해 국면/섹터 등 재사용.

    반환: {label: {pnl, loss, n, wins, win_rate, loss_share_pct}}
      - loss: 라벨 내 손실 거래 pnl 합(<=0)
      - loss_share_pct: 전체 실현손실 중 이 라벨 비중(%). 전체 손실 없으면 None.
    """
    agg: dict[str, dict] = {}
    for r in rts:
        lb = label_fn(r) or UNLABELED
        a = agg.setdefault(lb, {"pnl": 0.0, "loss": 0.0, "n": 0, "wins": 0})
        a["pnl"] += r["pnl"]
        a["n"] += 1
        if r["pnl"] > 0:
            a["wins"] += 1
        elif r["pnl"] < 0:
            a["loss"] += r["pnl"]
    total_loss = sum(a["loss"] for a in agg.values())
    for a in agg.values():
        a["pnl"] = round(a["pnl"])
        a["loss"] = round(a["loss"])
        a["win_rate"] = round(a["wins"] / a["n"] * 100, 1) if a["n"] else None
        a["loss_share_pct"] = (round(a["loss"] / total_loss * 100)
                               if total_loss else None)
    return agg


# ─── 엣지 분석 (v1.4, 순수) ──────────────────────────
# 나만의 퀀트 규칙 발굴: 보유기간·매수요일·재진입 컷 + 규칙 후보 전방(forward) 검증.
# ⚠️ 표본이 작을 땐(†) 가설일 뿐 — EDGE_RULES since 이후 표본으로 검증한다.

_KOR_WD = ["월", "화", "수", "목", "금", "토", "일"]


def weekday_label(rt: dict) -> str:
    """매수요일 라벨. 파싱 실패 시 UNLABELED."""
    d = _parse_dt(rt.get("buy_at"))
    return f"{_KOR_WD[d.weekday()]}요일" if d else UNLABELED


def hold_bucket_label(rt: dict) -> str:
    """보유기간 버킷. hold_days 없으면 UNLABELED."""
    h = rt.get("hold_days")
    if h is None:
        return UNLABELED
    if h <= 3:
        return "0-3일"
    if h <= 7:
        return "4-7일"
    if h <= 14:
        return "8-14일"
    return "15일+"


def batch_entry_flags(rts: list[dict], min_n: int = 4) -> list[bool]:
    """같은 날 min_n종목 이상 일괄 신규 진입 여부(순수).

    2026-07-09 교란 분석: '목요일의 저주'로 보였던 -836만의 실체는
    6/18·7/2 두 날의 8종목 일괄 매수(타이밍 몰빵)였다.
    """
    per_day: dict[str, int] = defaultdict(int)
    for r in rts:
        per_day[(r.get("buy_at") or "")[:10]] += 1
    return [per_day[(r.get("buy_at") or "")[:10]] >= min_n for r in rts]


def reentry_flags(rts: list[dict], days: int = 30) -> list[bool]:
    """각 라운드트립이 '같은 종목 손절 후 days일 내 재진입'인지 (rts 순서 정렬 무관, 순수)."""
    stops: dict[str, list] = defaultdict(list)
    for r in rts:
        if r["reason"] == "손절":
            d = _parse_dt(r.get("sell_at"))
            if d:
                stops[r["ticker"]].append(d)
    out = []
    for r in rts:
        b = _parse_dt(r.get("buy_at"))
        out.append(bool(b) and any(0 < (b - s).days <= days
                                   for s in stops.get(r["ticker"], [])))
    return out


# 규칙 후보 — 2026-07-09 탐색 분석에서 도출·정제한 가설. since 이후 매수분이 전방 검증 표본.
# 기각된 후보: '목요일 자제'(교란 — 실체는 6/18·7/2 일괄 매수), '일괄 진입 자제'(42건 중
# 40건이 일괄이라 판별력 없음 — 봇이 원래 배치로 삼). 판별력 있는 2개만 유지.
EDGE_RULES = [
    {"id": "R1", "desc": "손절 후 30일 내 같은 종목 재진입 금지", "since": "2026-07-09",
     "violate": lambda rt, ctx: ctx["reentry"].get(id(rt), False)},
    {"id": "R2", "desc": "Slowdown/Contraction 국면 신규 진입 축소", "since": "2026-07-09",
     "violate": lambda rt, ctx: ctx["phase_map"].get(
         (rt.get("buy_at") or "")[:7]) in ("Slowdown", "Contraction")},
]


def _agg(rts: list[dict]) -> dict:
    n = len(rts)
    wins = sum(1 for r in rts if r["pnl"] > 0)
    return {"n": n, "pnl": round(sum(r["pnl"] for r in rts)),
            "win_rate": round(wins / n * 100, 1) if n else None}


def edge_rules_report(rts: list[dict],
                      phase_map: Optional[dict] = None) -> list[dict]:
    """규칙별 위반/준수 성과 — 전체(참고)와 since 이후(전방 검증) 분리(순수).

    반환 각 항목: {id, desc, since, all_viol, all_comp, fwd_viol, fwd_comp}
    """
    flags = reentry_flags(rts)
    bflags = batch_entry_flags(rts)
    ctx = {"reentry": {id(r): f for r, f in zip(rts, flags)},
           "batch": {id(r): f for r, f in zip(rts, bflags)},
           "phase_map": phase_map if phase_map is not None else PHASE_BY_MONTH}
    out = []
    for rule in EDGE_RULES:
        viol = [r for r in rts if rule["violate"](r, ctx)]
        comp = [r for r in rts if not rule["violate"](r, ctx)]
        fwd = [r for r in rts if (r.get("buy_at") or "") >= rule["since"]]
        fv = [r for r in fwd if rule["violate"](r, ctx)]
        fc = [r for r in fwd if not rule["violate"](r, ctx)]
        out.append({"id": rule["id"], "desc": rule["desc"], "since": rule["since"],
                    "all_viol": _agg(viol), "all_comp": _agg(comp),
                    "fwd_viol": _agg(fv), "fwd_comp": _agg(fc)})
    return out


def format_edge(rts: list[dict], phase_map: Optional[dict] = None) -> str:
    """엣지 분석 섹션(순수). 빈 데이터면 안내문."""
    if not rts:
        return "🕵️ 엣지 분석: 완결된 거래가 아직 없습니다."
    lines = ["🕵️ 엣지 분석 (나만의 퀀트 후보 — †는 참고용)"]
    lines.append(f"• 보유기간: {_fmt_corr_line(correlate_pnl_by(rts, hold_bucket_label))}")
    lines.append(f"• 매수요일: {_fmt_corr_line(correlate_pnl_by(rts, weekday_label))}")
    flags = reentry_flags(rts)
    re_ = _agg([r for r, f in zip(rts, flags) if f])
    new = _agg([r for r, f in zip(rts, flags) if not f])
    if re_["n"]:
        lines.append(
            f"• 손절 후 30일 내 재진입: {re_['n']}건 {_won(re_['pnl'])} "
            f"승률{re_['win_rate']}% vs 신규 {new['n']}건 {_won(new['pnl'])} "
            f"승률{new['win_rate']}%")
    bflags = batch_entry_flags(rts)
    ba = _agg([r for r, f in zip(rts, bflags) if f])
    solo = _agg([r for r, f in zip(rts, bflags) if not f])
    if ba["n"] and solo["n"]:
        lines.append(
            f"• 일괄 진입(같은날 4종목+): {ba['n']}건 {_won(ba['pnl'])} "
            f"승률{ba['win_rate']}% vs 단독 {solo['n']}건 {_won(solo['pnl'])} "
            f"승률{solo['win_rate']}%")
    lines.append("• 규칙 전방검증:")
    for r in edge_rules_report(rts, phase_map):
        fwd_n = r["fwd_viol"]["n"] + r["fwd_comp"]["n"]
        if fwd_n:
            fwd = (f"등록후 위반 {r['fwd_viol']['n']}건 {_won(r['fwd_viol']['pnl'])} / "
                   f"준수 {r['fwd_comp']['n']}건 {_won(r['fwd_comp']['pnl'])}")
        else:
            fwd = "등록후 표본 없음"
        lines.append(
            f"  {r['id']} {r['desc']} (등록 {r['since']}): {fwd}\n"
            f"     과거참고 — 위반 {r['all_viol']['n']}건 {_won(r['all_viol']['pnl'])} "
            f"승률{r['all_viol']['win_rate']}% / 준수 {r['all_comp']['n']}건 "
            f"{_won(r['all_comp']['pnl'])} 승률{r['all_comp']['win_rate']}%")
    tags = format_tag_performance(rts)
    if tags:
        lines.append(tags)
    lines.append("  ※ 통계 관찰이며 투자 권유 아님. 전방 표본이 쌓인 뒤 규칙화 판단.")
    return "\n".join(lines)


def edge_report() -> str:
    """실제 paper.db → 엣지 분석 리포트(I/O 경계)."""
    try:
        import paper_db
        rts = compute_roundtrips(paper_db.list_trades(limit=100000))
    except Exception as e:
        log.exception("엣지 분석 조회 실패")
        return f"🕵️ 엣지 분석 조회 실패: {e}"
    return format_edge(rts, phase_map=merged_phase_map())


# ─── 마이퀀트 태그 (v1.5, 순수) ──────────────────────
# paper UI '나만의 퀀트' 탭 매수 시 notes에 "MQ[태그1,태그2]" 기록 → 태그별 성과 추적.

_TAG_RE = re.compile(r"MQ\[([^\]]*)\]")


def extract_tags(notes: Optional[str]) -> list[str]:
    """매수 notes → 마이퀀트 태그 리스트(순수). 없으면 []."""
    m = _TAG_RE.search(notes or "")
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def tag_performance(rts: list[dict]) -> dict[str, dict]:
    """태그별 성과 집계 — 한 거래가 여러 태그를 가지면 각 태그에 중복 계상(순수)."""
    by_tag: dict[str, list] = defaultdict(list)
    for r in rts:
        for tag in extract_tags(r.get("buy_notes")):
            by_tag[tag].append(r)
    return {tag: _agg(rs) for tag, rs in by_tag.items()}


def format_tag_performance(rts: list[dict]) -> str:
    """마이퀀트 태그별 성과 섹션(순수). 태그 거래 없으면 빈 문자열."""
    tp = tag_performance(rts)
    if not tp:
        return ""
    lines = ["🏷️ 마이퀀트 태그별 성과 (†는 표본 5건 미만)"]
    for tag, a in sorted(tp.items(), key=lambda kv: -(kv[1]["pnl"])):
        mark = "†" if a["n"] < MIN_SAMPLE_N else ""
        lines.append(f"• {tag}{mark}: {a['n']}건 {_won(a['pnl'])} "
                     f"승률 {a['win_rate']}%")
    return "\n".join(lines)


def top_loss_label(corr: dict[str, dict]) -> Optional[dict]:
    """손실 비중 최대 라벨({label, loss, loss_share_pct}). 손실 없으면 None."""
    losers = [(lb, a) for lb, a in corr.items() if a["loss"] < 0]
    if not losers:
        return None
    lb, a = min(losers, key=lambda kv: kv[1]["loss"])
    return {"label": lb, "loss": a["loss"], "loss_share_pct": a["loss_share_pct"]}


# ─── 포매팅 (순수) ───────────────────────────────────


def _won(n) -> str:
    n = int(round(n or 0))
    return f"{'+' if n > 0 else ''}{n:,}원"


def format_habit(h: dict) -> str:
    if not h["n_closed"]:
        return "🧪 매매 습관: 완결 거래가 없습니다."
    pf = h["profit_factor"]
    pf_s = "∞" if pf == float("inf") else ("-" if pf is None else f"{pf:.2f}")
    payoff = f"{h['payoff_ratio']:.2f}" if h["payoff_ratio"] is not None else "-"
    hold = f"{h['avg_hold_days']}일" if h["avg_hold_days"] is not None else "-"
    reasons = " · ".join(f"{k} {v}" for k, v in h["reason_counts"].items())
    return (
        "🧪 매매 습관\n"
        f"• 완결 {h['n_closed']}건 · 승 {h['wins']}/패 {h['losses']} "
        f"(승률 {h['win_rate']}%)\n"
        f"• 손익비(PF) {pf_s} · 손익크기비 {payoff} "
        f"(평균이익 {_won(h['avg_win'])} / 평균손실 {_won(h['avg_loss'])})\n"
        f"• 평균 보유 {hold} · 청산사유: {reasons}"
    )


def format_by_ticker(rows: list[dict], top: int = 3) -> str:
    if not rows:
        return "📊 종목별 손익: 데이터 없음."
    lines = ["📊 종목별 실현손익"]
    for r in rows[:top]:
        lines.append(f"  📈 {r['name']}: {_won(r['pnl'])} ({r['n']}회)")
    if len(rows) > top:
        for r in rows[-min(top, len(rows) - top):]:
            lines.append(f"  📉 {r['name']}: {_won(r['pnl'])} ({r['n']}회)")
    return "\n".join(lines)


def format_monthly(m: dict) -> str:
    if not m:
        return "📅 월별 손익: 데이터 없음."
    lines = ["📅 월별 실현손익"]
    for k, v in m.items():
        lines.append(f"  {k}: {_won(v)}")
    return "\n".join(lines)


def format_precision(rts: list[dict], trades: Optional[list] = None) -> str:
    """정밀 진단 섹션(손절 품질·휩쏘·손실 집중). 신호 없으면 빈 문자열."""
    sl = stop_loss_diagnosis(rts)
    wh = whipsaw(trades, rts) if trades else []
    lc = loss_concentration(rts)
    lines = ["🔎 정밀 진단"]
    if sl.get("n"):
        tag = ("⚠️ 지연(손절선보다 깊게 실행)" if sl["avg_ret"] < sl["threshold"]
               else "정상 범위")
        lines.append(
            f"• 손절 {sl['n']}건 평균 {sl['avg_ret']}% (손절선 {sl['threshold']}%) → {tag}\n"
            f"  손절선보다 깊게 실행 {sl['deeper_than_stop']}건 "
            f"(5%p+ 초과 {sl['much_deeper']}건)")
    if wh:
        names = ", ".join(f"{w['name']}({w['days']}일)" for w in wh)
        lines.append(f"• 휩쏘(손절 후 재매수) {len(wh)}건: {names}")
    if lc.get("n_losers"):
        tops = ", ".join(f"{t['name']} {_won(t['pnl'])}" for t in lc["top"])
        lines.append(
            f"• 손실 집중: 워스트{len(lc['top'])}이 총손실의 "
            f"{lc['top_share_pct']}% ({tops})")
    cmp = slippage_comparison(rts)
    if cmp["before"].get("n") and cmp["after"].get("n"):
        b, a = cmp["before"], cmp["after"]
        lines.append(
            f"• 실행지연 수정({cmp['cutoff']}) 전/후 손절: "
            f"평균 {b['avg_ret']}%({b['n']}건) → {a['avg_ret']}%({a['n']}건) · "
            f"슬리피지 {b['slippage']}%p → {a['slippage']}%p")
    return "\n".join(lines) if len(lines) > 1 else ""


MIN_SAMPLE_N = 5  # 이 미만 라벨은 참고용(†) 표시


def _fmt_corr_line(corr: dict[str, dict]) -> str:
    """상관 집계 → '라벨 pnl(n건·승률%)' 한 줄(pnl 오름차순 = 손실 먼저). n<5는 †."""
    items = sorted(corr.items(), key=lambda kv: kv[1]["pnl"])
    return " · ".join(
        f"{lb}{'†' if a['n'] < MIN_SAMPLE_N else ''} "
        f"{_won(a['pnl'])}({a['n']}건 승률{a['win_rate']}%)"
        for lb, a in items)


def format_correlation(rts: list[dict],
                       phase_fn: Optional[Callable[[dict], str]] = None,
                       sector_fn: Optional[Callable[[dict], str]] = None) -> str:
    """국면·섹터 상관 섹션(순수). 데이터 없으면 빈 문자열."""
    if not rts:
        return ""
    sec_fn = sector_fn or sector_label
    pc = correlate_pnl_by(rts, phase_fn or phase_label)
    sc = correlate_pnl_by(rts, sec_fn)
    unmapped = sorted({r["name"] for r in rts if sec_fn(r) == UNLABELED})
    lines = ["🧭 국면·섹터 상관 (국면=월별 수동 근사)"]
    lines.append(f"• 국면별: {_fmt_corr_line(pc)}")
    lines.append(f"• 섹터별: {_fmt_corr_line(sc)}")
    tp, ts = top_loss_label(pc), top_loss_label(sc)
    if tp and ts:
        lines.append(
            f"• 손실 집중: {tp['label']} 국면에 {tp['loss_share_pct']}% · "
            f"{ts['label']} 섹터에 {ts['loss_share_pct']}%")
    if unmapped:
        lines.append(f"• ⚠️ 섹터 미분류 {len(unmapped)}종목: {', '.join(unmapped)} "
                     "— SECTOR_BY_TICKER에 추가 필요")
    if any(a["n"] < MIN_SAMPLE_N for a in list(pc.values()) + list(sc.values())):
        lines.append(f"  († = 표본 {MIN_SAMPLE_N}건 미만 — 참고용)")
    return "\n".join(lines)


def format_deep(rts: list[dict], history: Optional[list] = None,
                trades: Optional[list] = None,
                phase_map: Optional[dict] = None) -> str:
    if not rts:
        return "🔬 매매 심화 분석: 완결된 거래가 아직 없습니다."
    h = habit_stats(rts)
    parts = ["🔬 매매 심화 분석", format_habit(h),
             format_by_ticker(by_ticker(rts)), format_monthly(monthly_pnl(rts))]
    prec = format_precision(rts, trades)
    if prec:
        parts.append(prec)
    corr = format_correlation(
        rts, phase_fn=(lambda r: phase_label(r, phase_map))
        if phase_map is not None else None)
    if corr:
        parts.append(corr)
    if history and len(history) >= 2:
        first, last = history[0], history[-1]
        parts.append(f"📈 스냅샷 추세: {first.get('ts')} → {last.get('ts')} "
                     f"({len(history)}개 기록 누적)")
    return "\n\n".join(parts)


# ─── 4) LLM 자연어 총평 (주입 가능) ──────────────────


def _default_llm(prompt: str) -> str:
    from urllib.request import Request, urlopen
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "keep_alive": "30m",
        "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 500},
    }).encode("utf-8")
    req = Request(f"{OLLAMA_URL}/api/chat", data=body,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"].strip()


#: LLM에게 주는 규칙. **수치를 지어내는 것이 이 총평의 유일한 실패 모드다.**
#: 표본 63건짜리 통계를 놓고 모델이 "3분기 들어" 같은 없는 구간이나 반올림하지
#: 않은 숫자를 만들어 내면, 사람은 그것이 계산된 값인지 생성된 값인지 구분할 수
#: 없다. 그래서 **주어진 숫자만 쓰라고 명시하고, 화면에는 원자료를 함께 싣는다.**
_LLM_RULES = (
    "규칙:\n"
    "1) **아래 제공된 숫자만 사용하라.** 새로운 수치·비율·기간을 만들어 내지 말 것.\n"
    "2) 제공되지 않은 것은 '자료 없음'이라고 쓰라. 추정하지 말 것.\n"
    "3) 표본이 적으면 단정하지 말고 '표본이 적어 단정하기 어렵다'고 쓰라.\n"
    "4) 투자 권유·종목 추천을 하지 말 것.\n"
)


def llm_summary(rts: list[dict], llm_fn: Optional[Callable[[str], str]] = None,
                trades: Optional[list] = None, n_excluded: int = 0) -> str:
    """수치 분석 → 로컬 LLM 한국어 총평. 실패 시 빈 문자열(graceful).

    `n_excluded`를 함께 넘긴다. 품질 규칙으로 뺀 건수를 모델이 모르면 "손실이
    크다"는 진단을 남은 표본에 대해 내리는데, 실제로 그 손실은 뺀 쪽에 있었다.
    """
    if not rts:
        return ""
    h = habit_stats(rts)
    tk = by_ticker(rts)
    sl = stop_loss_diagnosis(rts)
    lc = loss_concentration(rts)
    wh = whipsaw(trades, rts) if trades else []
    scope = (f"- 표본: 완결 {h['n_closed']}건. 가격 오류 등 품질 규칙으로 "
             f"{n_excluded}건을 집계에서 제외한 뒤의 수치다\n" if n_excluded else "")
    prompt = (
        "다음은 한 개인 투자자의 모의(paper) 매매 통계다. 한국어로 4~6줄, 과장 없이 "
        "무엇이 잘/못 되고 있는지 진단하고 구체적 개선점 1~2개를 제시하라.\n\n"
        + _LLM_RULES + "\n" + scope +
        f"- 완결 {h['n_closed']}건, 승률 {h['win_rate']}%, 손익비(PF) {h['profit_factor']}, "
        f"평균이익 {h['avg_win']} / 평균손실 {h['avg_loss']}\n"
        f"- 청산사유: {h['reason_counts']}, 평균 보유일 {h['avg_hold_days']}\n"
        f"- 손절 실행 평균 {sl.get('avg_ret')}% (손절선 {STOP_LOSS_PCT}%), "
        f"손절선보다 깊게 실행 {sl.get('deeper_than_stop')}건, 휩쏘 {len(wh)}건\n"
        f"- 손실 집중: 워스트3이 총손실의 {lc.get('top_share_pct')}%\n"
        f"- 국면별 손익(수동 근사): "
        f"{ {lb: a['pnl'] for lb, a in correlate_pnl_by(rts, phase_label).items()} }\n"
        f"- 섹터별 손익: "
        f"{ {lb: a['pnl'] for lb, a in correlate_pnl_by(rts, sector_label).items()} }\n"
        f"- 종목 베스트: {[(r['name'], r['pnl']) for r in tk[:3]]}\n"
        f"- 종목 워스트: {[(r['name'], r['pnl']) for r in tk[-3:]]}\n"
        f"- 월별 손익: {monthly_pnl(rts)}\n"
    )
    try:
        return (llm_fn or _default_llm)(prompt)
    except Exception as e:
        log.warning(f"LLM 총평 실패 — 생략: {e}")
        return ""


# ─── DB 연동 ─────────────────────────────────────────


def deep_report(db_path=None, include_llm: bool = True,
                llm_fn: Optional[Callable[[str], str]] = None) -> str:
    """실제 paper.db 거래 → 심화 분석 리포트(+정밀 진단 +선택 LLM 총평).

    **2026-08-31: 여기서 `compute_roundtrips`(원본)를 쓰고 있었다.** 성과 분석인데
    품질 규칙을 적용하지 않아 스테일 시세 16건이 그대로 섞였다. 차이가 작지 않다.

        원본 79건   PF 0.76   누적 −5,923,267원
        필터 63건   PF 0.97   누적 −535,292원   (제외분 합계 −5,387,975원)

    제외분이 손실의 90%를 차지한다. 그 상태로 LLM 총평을 만들면 **시장이 낸 손실이
    아니라 가격 오류가 낸 손실을 놓고 전략을 진단한다.** 그래서 성과 경로는
    `roundtrips_for_analysis`를 쓰고, 무엇을 뺐는지 리포트에 함께 적는다.
    """
    try:
        import paper_db
        trades = (paper_db.list_trades(limit=100000, db_path=db_path) if db_path
                  else paper_db.list_trades(limit=100000))
    except Exception as e:
        log.exception("거래 조회 실패")
        return f"🔬 매매 분석 조회 실패: {e}"
    rts, dropped = roundtrips_for_analysis(trades)
    history = None
    try:
        import paper_analytics
        history = paper_analytics.load_history()
    except Exception:
        pass
    report = format_deep(rts, history, trades, phase_map=merged_phase_map())
    if dropped:
        try:
            import data_quality
            note = data_quality.format_summary(data_quality.summary(dropped))
            if note:
                report += "\n\n" + note
        except Exception as e:  # noqa: BLE001
            log.warning("제외 요약 생성 실패: %s", e)
    if include_llm and rts:
        narrative = llm_summary(rts, llm_fn, trades, n_excluded=len(dropped))
        if narrative:
            report += "\n\n🧠 총평\n" + narrative
    return report


def run(**kwargs) -> tuple[str, list]:
    return deep_report(), []


if __name__ == "__main__":
    import sys
    print(deep_report(include_llm="--llm" in sys.argv))
