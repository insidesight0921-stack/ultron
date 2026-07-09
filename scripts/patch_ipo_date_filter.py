#!/usr/bin/env python3
"""
patch_ipo_date_filter.py — fetch_ipo_schedule_38 날짜 필터 + scan 실행 검증 (v3.27)

변경 내용:
  - fetch_ipo_schedule_38: days_ahead 이내 청약 일정만 반환 (과거 데이터 제거)
  - verify_parsers.py: EUC-KR 파일도 처리하도록 수정

사용:
  cd ~/울트론/ai-agent
  python outputs/patch_ipo_date_filter.py          # dry-run
  python outputs/patch_ipo_date_filter.py --apply  # 적용
  python scripts/ipo_bot.py scan                   # 실제 스캔 확인
"""
import sys
import shutil
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "scripts" / "ipo_bot.py"


# ── 패치: fetch_ipo_schedule_38 날짜 필터 추가 ──────────────────────────────

OLD_FETCH_38 = '''\
def fetch_ipo_schedule_38(days_ahead: int = 30) -> list[IpoItem]:
    """38커뮤니케이션에서 청약 일정 수집.

    TODO: _parse_38_html 완성 후 실제 데이터 반환.
    """
    html = _fetch_38_html()
    if not html:
        return []
    items = _parse_38_html(html)
    log.info(f"38커뮤니케이션: {len(items)}건 수집")
    return items'''

NEW_FETCH_38 = '''\
def fetch_ipo_schedule_38(days_ahead: int = 30) -> list[IpoItem]:
    """38커뮤니케이션에서 청약 일정 수집 (days_ahead 이내만 반환).

    38 목록 페이지는 과거 데이터도 포함하므로 sub_start 기준 날짜 필터 적용.
    sub_start가 없는 종목(미확정)은 포함.
    """
    html = _fetch_38_html()
    if not html:
        return []
    items = _parse_38_html(html)
    # days_ahead 이내 청약 예정만 필터 (sub_start 기준)
    today = date.today()
    end   = today + timedelta(days=days_ahead)
    today_str = today.strftime("%Y%m%d")
    end_str   = end.strftime("%Y%m%d")
    filtered = [
        it for it in items
        if (not it.sub_start) or (today_str <= it.sub_start <= end_str)
    ]
    log.info(
        f"38커뮤니케이션: {len(filtered)}건 "
        f"({days_ahead}일 이내 / 전체 {len(items)}건)"
    )
    return filtered'''


PATCHES = [
    ("fetch_ipo_schedule_38 날짜 필터", OLD_FETCH_38, NEW_FETCH_38),
]


def main():
    apply = "--apply" in sys.argv

    if not TARGET.exists():
        print(f"❌ 파일 없음: {TARGET}")
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")
    patched = src

    results = []
    for name, old, new in PATCHES:
        old_c = old.replace("\\\\", "\\")
        new_c = new.replace("\\\\", "\\")
        if old_c in patched:
            patched = patched.replace(old_c, new_c, 1)
            results.append((name, True))
        else:
            results.append((name, False))

    print("=" * 60)
    print(f"패치 대상: {TARGET}")
    print()
    for name, ok in results:
        icon = "✅" if ok else "❌ (이미 적용됐거나 불일치)"
        print(f"  {icon}  {name}")

    if patched == src:
        print("\n⚠️  변경 없음.")
        return

    if not apply:
        print(f"\n🔍 dry-run. 실제 적용: --apply")
        return

    backup = TARGET.with_suffix(".py.bak2")
    shutil.copy2(TARGET, backup)
    print(f"\n  백업: {backup.name}")
    TARGET.write_text(patched, encoding="utf-8")
    print("  ✅ 패치 적용 완료")
    print("\n다음 명령어로 전체 스캔 실행:")
    print("  python scripts/ipo_bot.py scan")
    print("  python scripts/ipo_bot.py scan --days 60  # 60일 범위")


if __name__ == "__main__":
    main()
