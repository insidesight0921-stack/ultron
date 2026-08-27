"""seed_watchlist_from_bots.py — 콴텍/키움 상위 종목을 관심종목에 담는 일회성 도구 (v1)

signal_bot이 관심종목까지 스캔하게 되면서, "무엇을 볼지"를 채워 넣는 손이 필요해졌다.
두 봇의 현재 상위 종목을 그대로 관심종목에 넣는다.

  콴텍봇 — 거시 국면 판단 후 국면별 팩터 가중 z-score 합산 Top N
  키움봇 — 12-1 모멘텀 점수 Top N

이 도구는 **미리보기(dry-run) 전용**이다. Private API 승인 흐름을 우회하는 직접 DB
기록은 제공하지 않는다. 실제 추가는 텔레그램의 관심종목 명령으로 각각 승인한다.

주의:
  - 관심종목은 비중 없이 '관찰만' 하는 목록이다. 자산배분(핵심_자산배분_포트폴리오.md)과
    다른 성격이며, 여기 넣는다고 매매나 배분이 생기지 않는다.
  - 두 봇의 점수는 **실행 시점 기준**이다. 시간이 지나면 순위가 바뀐다.
    한 번 담은 종목은 자동으로 빠지지 않으므로, 주기적으로 정리하거나
    "종목 삭제"로 직접 뺀다.
  - 스캔은 외부 데이터(pykrx/ECOS/FRED)를 쓰므로 수십 초~수 분 걸린다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _bootstrap_env() -> None:
    """CLI 직접 실행 시 봇과 같은 환경을 갖춘다.

    텔레그램·paper_ui는 launchd가 환경을 넣어 주지만 이 스크립트는 맨손으로 뜬다.
      - .env 로드: ECOS/FRED 키가 없으면 콴텍 국면 판단이 통째로 실패한다.
      - Data API 플래그: KRX 구성종목 API가 빈 응답이라 키움 universe는
        8090 Shareable API 스냅샷에 의존한다. 플래그가 없으면 조회 자체를 건너뛴다.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        print("⚠️ python-dotenv 없음 — .env를 못 읽습니다(키 필요한 스캔은 실패할 수 있음)")
    os.environ.setdefault("AI_AGENT_DATA_API_ENABLED", "1")


_bootstrap_env()


def latest_cached_universe(market: str) -> tuple[list, str | None]:
    """가장 최근 universe 스냅샷 → (구성종목, 기준일). 없으면 ([], None).

    KRX 구성종목 API가 빈 응답을 주는 날이 잦다. 그때 스캔을 통째로 포기하는 대신
    마지막 스냅샷으로 돌린다. KOSPI200 구성은 분기 정기변경이라 며칠 차이는
    모멘텀 순위에 거의 영향이 없다. **다만 기준일을 반드시 출력해 오해를 막는다.**
    """
    import json
    from storage_paths import PATHS
    files = sorted(Path(PATHS.shareable_cache_dir).glob(f"universe_{market}_*.json"))
    if not files:
        return [], None
    newest = files[-1]
    try:
        rows = json.loads(newest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return [], None
    as_of = newest.stem.rsplit("_", 1)[-1]
    return [(str(r[0]), str(r[1])) for r in rows if len(r) >= 2], as_of


def kium_top(top_n: int, market: str, allow_stale_universe: bool = True) -> list[dict]:
    """키움봇 모멘텀 Top N → [{ticker, name, score, source}]."""
    from kium_bot import scan_universe
    try:
        rows = scan_universe(market=market, top_n=top_n)
    except Exception:
        if not allow_stale_universe:
            raise
        universe, as_of = latest_cached_universe(market)
        if not universe:
            raise
        print(f"   ⚠️ universe 최신 조회 실패 → {as_of} 스냅샷 {len(universe)}종목으로 진행")
        rows = scan_universe(market=market, top_n=top_n, universe=universe)
    return [{"ticker": r["ticker"], "name": r.get("name") or r["ticker"],
             "score": round(float(r.get("score") or 0), 3),
             "source": "kium"} for r in rows]


def quant_top(top_n: int, market: str, phase_override: str | None = None) -> list[dict]:
    """콴텍봇 팩터 Top N → [{ticker, name, score, source}]."""
    import quant_bot as qb
    snap = qb.snapshot()
    phase = phase_override if phase_override in qb.PHASES else snap.consensus_phase
    if not phase:
        raise RuntimeError("거시 국면 미확정 — ECOS/FRED 키·네트워크 확인 "
                           "(--phase 로 강제 지정 가능)")
    # weights는 phase별 dict가 아니라 {phase: {...}} 전체를 넘긴다 —
    # recommend_top_n이 내부에서 weights.get(phase)로 꺼낸다.
    weights_all = qb.parse_phase_weights_from_wiki()
    recs = qb.recommend_top_n(phase, market=market, top_n=top_n,
                              weights=weights_all)
    return [{"ticker": r.ticker, "name": r.name or r.ticker,
             "score": round(r.composite_score, 3), "source": f"quant:{phase}"}
            for r in recs]


def merge(rows: list[dict]) -> list[dict]:
    """티커 기준 중복 제거. 두 봇에 모두 걸린 종목은 source를 합친다."""
    out: dict[str, dict] = {}
    for row in rows:
        key = row["ticker"]
        if key in out:
            prev = out[key]["source"]
            if row["source"] not in prev:
                out[key]["source"] = f"{prev}+{row['source']}"
            continue
        out[key] = dict(row)
    return list(out.values())


def main() -> int:
    ap = argparse.ArgumentParser(description="콴텍·키움 상위 종목을 관심종목에 담기")
    ap.add_argument("--top", type=int, default=10, help="봇별 상위 N개 (기본 10)")
    ap.add_argument("--market", default="KOSPI200", help="universe (기본 KOSPI200)")
    ap.add_argument("--phase", help="콴텍 국면 강제 지정 (Recovery/Expansion/Slowdown/Contraction)")
    ap.add_argument("--only", choices=["quant", "kium"], help="한쪽만 실행")
    ap.add_argument("--no-stale-universe", action="store_true",
                    help="universe 최신 조회 실패 시 과거 스냅샷으로 대체하지 않음")
    args = ap.parse_args()

    rows: list[dict] = []
    if args.only != "quant":
        print(f"⏳ 키움봇 모멘텀 스캔 ({args.market}, Top {args.top})...")
        try:
            rows += kium_top(args.top, args.market,
                             allow_stale_universe=not args.no_stale_universe)
        except Exception as e:  # noqa: BLE001
            print(f"❌ 키움 스캔 실패: {e}")
    if args.only != "kium":
        print(f"⏳ 콴텍봇 팩터 스캔 ({args.market}, Top {args.top})...")
        try:
            rows += quant_top(args.top, args.market, args.phase)
        except Exception as e:  # noqa: BLE001
            print(f"❌ 콴텍 스캔 실패: {e}")

    if not rows:
        print("결과 없음 — 추가할 종목이 없습니다.")
        return 1

    merged = merge(rows)
    print(f"\n📋 대상 {len(merged)}종목 (중복 제거 전 {len(rows)}건)")
    for r in merged:
        print(f"  {r['ticker']}  {r['name']:<14} score={r['score']}  [{r['source']}]")

    print("\n미리보기 전용입니다. 실제 추가는 텔레그램 관심종목 명령으로 승인해 주세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
