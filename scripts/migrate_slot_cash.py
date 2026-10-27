#!/usr/bin/env python3
"""migrate_slot_cash.py — 슬롯 현금을 「시드 + 거래 기록」에 맞춘다 (운영자 CLI, 2026-10-27)

**무엇이 어긋났나.** 2026-08-29 `migrate_slot_allocation`이 콴텍·키움 비중을 40% → 35%로
고쳤다(시드 4,000만 → 3,500만). 그때 "`current_capital`은 실제 매매의 결과이므로 그대로
둔다"고 했는데, current_capital에는 매매 결과만이 아니라 **처음 넣은 시드 4,000만**이
들어 있었다. 그래서 두 슬롯 현금이 시드 기준보다 500만원씩(합계 1,000만원) 많았다.

    거래 기록으로 다시 센 현금  콴텍 33,978,890   키움 11,547,198
    DB current_capital         콴텍 38,978,890   키움 16,547,198   (각 +5,000,000)

이 차이는 화면만의 문제가 아니다 — 봇이 current_capital로 매수 금액을 정하므로
없는 500만원으로 사고 있었다. 화면의 총 평가자산도 1억 시드 위에 1,000만원이 얹혀
102.9M(+2.9%)로 보였지만, 자산곡선(`equity_curve`, 시드 기준)은 약 −8%였다.

**어떻게 고치나.** 현금 = 시드(비중 × 포트폴리오 시드) − 매수(수량×가격+수수료)
+ 매도(수량×가격−수수료). `paper_db.buy/sell`이 current_capital을 움직이는 식과 같다.
차이만큼 **상대 갱신**(`current_capital = current_capital − 차이`)한다 — 장중에 봇이
같은 행을 바꿔도 덮어쓰지 않는다. 되돌리기도 파일 복사가 아니라 기록해 둔 차이를
더하는 상대 갱신이다(복사로 되돌리면 그 사이 체결이 사라진다). 안전망으로 SQLite
backup API 사본도 뜬다(WAL 모드라 파일 복사로는 최근 기록이 빠질 수 있다).

**적용 뒤 private-data-api를 재시작해야 한다(2026-10-27 실측).** 그 서비스가 paper.db의
유일한 상시 연결인데, 9월 초부터 지워진 -shm(WAL 인덱스)을 붙든 채 돌고 있었다. 이 스크립트가
연결을 열면 SQLite가 인덱스를 새로 만들고, 서비스는 그 뒤 기록을 보지 못한다 — 그대로 두면
서비스가 낡은 인덱스로 쓰다가 이 정정을 덮어쓸 수 있다. 재시작 후 integrity_check는 ok였다.

사용법:
    python3 scripts/migrate_slot_cash.py            # 미리보기(기본)
    python3 scripts/migrate_slot_cash.py --apply    # 적용
    python3 scripts/migrate_slot_cash.py --restore <기록 json>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TOL = 1.0   # 원 — 부동소수 누적 오차는 고치지 않는다


def expected_cash(seed: float, trades: list[dict]) -> float:
    """시드 + 거래 기록 → 지금 있어야 할 현금(순수)."""
    cash = float(seed)
    for t in trades:
        qty, price, fees = int(t["quantity"] or 0), float(t["price"] or 0), float(t["fees"] or 0)
        if t["side"] == "buy":
            cash -= qty * price + fees
        elif t["side"] == "sell":
            cash += qty * price - fees
    return cash


def plan(slots: list[dict], trades: list[dict], seed_total: float, *, tol: float = TOL) -> list[dict]:
    """슬롯별 (DB 현금, 있어야 할 현금, 차이)(순수). 차이가 tol 이하인 슬롯은 뺀다."""
    out = []
    for s in slots:
        mine = [t for t in trades if t["slot_id"] == s["id"]]
        want = expected_cash(float(s["allocation_pct"]) * seed_total, mine)
        diff = float(s["current_capital"]) - want
        if abs(diff) > tol:
            out.append({"id": s["id"], "name": s["name"], "db": float(s["current_capital"]),
                        "expected": want, "diff": diff})
    return out


def _read(con: sqlite3.Connection) -> tuple[list[dict], list[dict], float]:
    con.row_factory = sqlite3.Row
    slots = [dict(r) for r in con.execute(
        "SELECT id, name, allocation_pct, current_capital FROM slots ORDER BY id")]
    trades = [dict(r) for r in con.execute(
        "SELECT slot_id, side, quantity, price, fees FROM trades")]
    seed = float(con.execute("SELECT seed_capital FROM portfolios LIMIT 1").fetchone()[0])
    return slots, trades, seed


def preview(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return plan(*_read(con))
    finally:
        con.close()


def apply(db: Path, log_dir: Path) -> tuple[list[dict], Path, Path]:
    """한 트랜잭션 안에서 다시 계산하고 상대 갱신한다. (적용분, 사본, 기록)."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = db.with_name(f"{db.stem}.cash-{stamp}.bak{db.suffix}")
    con = sqlite3.connect(str(db), timeout=30)
    try:
        dst = sqlite3.connect(str(backup))
        con.backup(dst)
        dst.close()
        con.execute("BEGIN IMMEDIATE")              # 계산~갱신 사이에 체결이 끼지 않게
        items = plan(*_read(con))
        for it in items:
            con.execute("UPDATE slots SET current_capital = current_capital - ? WHERE id = ?",
                        (it["diff"], it["id"]))
        left = plan(*_read(con))
        if left:
            con.rollback()
            raise SystemExit(f"❌ 적용 후에도 어긋남 — 되돌렸습니다: {left}")
        con.commit()
    finally:
        con.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    record = log_dir / f"slot_cash_fix_{stamp}.json"
    record.write_text(json.dumps({"at": stamp, "db": str(db), "backup": str(backup),
                                  "items": items}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return items, backup, record


def restore(db: Path, record: Path) -> list[dict]:
    """기록한 차이를 다시 더한다(상대 갱신)."""
    items = json.loads(record.read_text(encoding="utf-8"))["items"]
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        for it in items:
            con.execute("UPDATE slots SET current_capital = current_capital + ? WHERE id = ?",
                        (it["diff"], it["id"]))
        con.commit()
    finally:
        con.close()
    return items


def _print(items: list[dict]) -> None:
    if not items:
        print("✅ 모든 슬롯 현금이 시드 + 거래 기록과 일치합니다")
        return
    print(f"{'슬롯':10}{'DB 현금':>16}{'있어야 할 현금':>18}{'차이':>16}")
    for it in items:
        print(f"{it['name']:10}{it['db']:>16,.0f}{it['expected']:>18,.0f}{it['diff']:>+16,.0f}")


def main() -> int:
    import paper_db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="실제로 적용(기본은 미리보기)")
    ap.add_argument("--restore", help="적용 기록 json으로 되돌린다")
    ap.add_argument("--db", help="DB 경로(기본: paper_db.DEFAULT_DB_PATH)")
    args = ap.parse_args()

    db = Path(args.db) if args.db else Path(paper_db.DEFAULT_DB_PATH)
    if not db.exists():
        raise SystemExit(f"❌ DB 없음: {db}")
    if args.restore:
        items = restore(db, Path(args.restore))
        print(f"↩️  {len(items)}개 슬롯 현금을 되돌렸습니다 — private-data-api를 재시작하세요")
        _print(preview(db))
        return 0

    items = preview(db)
    print(f"DB: {db}\n")
    _print(items)
    if not items:
        return 0
    if not args.apply:
        print("\n(미리보기입니다. 적용하려면 --apply)")
        return 0
    done, backup, record = apply(db, db.parent / "migrations")
    print(f"\n✅ {len(done)}개 슬롯 적용 · 사본 {backup.name}")
    print(f"되돌리려면: python3 {Path(__file__).name} --restore {record}")
    print("⚠️ 지금 바로 private-data-api를 재시작하세요 — 그 전에는 서비스가 이 정정을 보지 못합니다:")
    print("   launchctl kickstart -k gui/$(id -u)/com.hyunjun.ai-agent.private-data-api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
