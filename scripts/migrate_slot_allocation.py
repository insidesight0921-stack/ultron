#!/usr/bin/env python3
"""migrate_slot_allocation.py — 슬롯 비중 합계를 100%로 정정 (운영자 CLI)

2026-08-29 실측: 합계가 **110%**였다(콴텍 40 + 키움 40 + IPO 20 + 마이퀀트 10).
마이퀀트가 `SLOT_DEFINITIONS`(40/40/20 = 100%)에 없이 `ensure_slot`으로 따로
추가되면서 얹혔다.

시드를 `비중 × 포트폴리오 시드`로 계산하므로 **넣지 않은 1,000만원 위에서**
수익률을 재고 있었다. 표시 오류가 아니다 — 전환 기준 판정이 뒤집힌다.

    포트폴리오 수익률   −5.94%  →  −8.15%
    초과수익           +2.09%p →  **−0.12%p**   (코스피를 이긴 게 아니라 비긴 것)
    MDD                12.53%  →  16.98%
    젠센 알파          −18.93% →  −24.51%

**비중만 바꾼다. 거래 기록·보유·현재자본은 건드리지 않는다.**
`current_capital`은 실제 매매의 결과이므로 그대로 둔다 — 비중은 시드 계산과
목표 배분에만 쓰인다.

**과거 수익률이 소급해서 달라진다.** 콴텍·키움 시드가 4,000만 → 3,500만이 되어
같은 손익이 더 큰 손실률로 나온다. 성적이 나빠 보이는 쪽이 맞는 값이다.

사용법:
    python3 scripts/migrate_slot_allocation.py            # 미리보기(기본)
    python3 scripts/migrate_slot_allocation.py --apply    # 적용
    python3 scripts/migrate_slot_allocation.py --restore <백업파일>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_db  # noqa: E402
import slot_allocation  # noqa: E402

TARGET = dict(paper_db.SLOT_DEFINITIONS)


def _rows(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, name, allocation_pct, current_capital FROM slots ORDER BY id")]
    finally:
        con.close()


def preview(db: Path) -> dict:
    rows = _rows(db)
    before = slot_allocation.check(rows)
    plan, unknown = [], []
    for r in rows:
        target = TARGET.get(r["name"])
        if target is None:
            unknown.append(r["name"])
            continue
        if abs(float(r["allocation_pct"]) - target) > 1e-9:
            plan.append({"id": r["id"], "name": r["name"],
                         "from": float(r["allocation_pct"]), "to": target})
    after = slot_allocation.check(
        [{"name": r["name"],
          "allocation_pct": TARGET.get(r["name"], r["allocation_pct"])}
         for r in rows])
    return {"rows": rows, "before": before, "after": after,
            "plan": plan, "unknown": unknown}


def _print(p: dict, seed: float) -> None:
    print(f"{'슬롯':10}{'현재':>8}{'변경':>8}{'현재 시드':>16}{'변경 시드':>16}")
    for r in p["rows"]:
        target = TARGET.get(r["name"])
        cur = float(r["allocation_pct"])
        tgt = cur if target is None else target
        mark = "" if target is not None else "  ← 정의에 없음"
        print(f"{r['name']:10}{cur*100:>7.0f}%{tgt*100:>7.0f}%"
              f"{cur*seed:>16,.0f}{tgt*seed:>16,.0f}{mark}")
    print(f"\n합계  {p['before']['total']*100:.0f}% → {p['after']['total']*100:.0f}%")
    if p["unknown"]:
        print(f"⚠️ SLOT_DEFINITIONS에 없는 슬롯: {', '.join(p['unknown'])} — 건드리지 않습니다")


def apply(db: Path, p: dict) -> Path:
    """백업을 먼저 뜨고 비중만 갱신한다."""
    if not p["after"]["ok"]:
        raise SystemExit(f"❌ 적용 후에도 합계가 맞지 않습니다: {p['after']['detail']}")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = db.with_name(f"{db.stem}.slots-{stamp}.bak{db.suffix}")
    shutil.copy2(db, backup)

    con = sqlite3.connect(str(db))
    try:
        for item in p["plan"]:
            con.execute("UPDATE slots SET allocation_pct = ? WHERE id = ?",
                        (item["to"], item["id"]))
        con.commit()
    finally:
        con.close()

    after = slot_allocation.check(_rows(db))
    if not after["ok"]:
        shutil.copy2(backup, db)
        raise SystemExit(f"❌ 검증 실패 — 백업으로 되돌렸습니다: {after['detail']}")
    return backup


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="실제로 적용(기본은 미리보기)")
    ap.add_argument("--restore", help="백업 파일로 되돌린다")
    ap.add_argument("--db", help="DB 경로(기본: paper_db.DEFAULT_DB_PATH)")
    args = ap.parse_args()

    db = Path(args.db) if args.db else Path(paper_db.DEFAULT_DB_PATH)
    if not db.exists():
        raise SystemExit(f"❌ DB 없음: {db}")

    if args.restore:
        src = Path(args.restore)
        if not src.exists():
            raise SystemExit(f"❌ 백업 없음: {src}")
        shutil.copy2(src, db)
        print(f"↩️  {src.name} 으로 되돌렸습니다")
        print(slot_allocation.check(_rows(db))["detail"])
        return 0

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    seed = float(con.execute("SELECT seed_capital FROM portfolios LIMIT 1").fetchone()[0])
    con.close()

    p = preview(db)
    print(f"DB: {db}\n포트폴리오 시드: {seed:,.0f}원\n")
    _print(p, seed)

    if not p["plan"]:
        print("\n✅ 바꿀 것이 없습니다")
        return 0
    if not args.apply:
        print("\n(미리보기입니다. 적용하려면 --apply)")
        print("※ 과거 수익률이 소급해서 달라집니다 — 콴텍·키움 시드가 줄어")
        print("  같은 손익이 더 큰 손실률로 나옵니다. 그쪽이 맞는 값입니다.")
        return 0

    backup = apply(db, p)
    print(f"\n✅ 적용 완료 · 백업 {backup.name}")
    print(slot_allocation.check(_rows(db))["detail"])
    print(f"되돌리려면: python3 {Path(__file__).name} --restore {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
