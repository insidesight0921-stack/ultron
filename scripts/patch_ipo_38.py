#!/usr/bin/env python3
"""
patch_ipo_38.py — _parse_38_html 컬럼 재매핑 패치 (v3.27)

발견된 실제 38.co.kr 컬럼 구조 (2026.05 기준):
  col0: 종목명 (링크, &nbsp; prefix)
  col1: 청약기간  "2026.06.18~06.19"
  col2: 확정공모가  "-" or "22,000"
  col3: 공모가범위  "22,000~27,000"
  col4: 기타 (비어있거나 텍스트)
  col5: 주관사  "미래에셋증권,이모아증권"
  col6: 분석 버튼 이미지 (skip)

사용:
  cd ~/울트론/ai-agent
  python outputs/patch_ipo_38.py          # 미리보기(dry-run)
  python outputs/patch_ipo_38.py --apply  # 실제 적용
"""
import re
import sys
import shutil
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "scripts" / "ipo_bot.py"


# ── 패치 1: _clean — &nbsp; 등 HTML 엔티티 처리 ──────────────────────────
OLD_CLEAN = '''\
def _clean(s: str) -> str:
    """HTML 태그·공백 제거."""
    return re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()'''

NEW_CLEAN = '''\
def _clean(s: str) -> str:
    """HTML 태그·공백·엔티티 제거."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"&[a-zA-Z]+;|&#\\d+;", "", s)
    return re.sub(r"\\s+", " ", s).strip()'''


# ── 패치 2: _parse_date_38 — YYYY.MM.DD 형식 지원 ───────────────────────
OLD_DATE_38 = '''\
def _parse_date_38(s: str, year: int) -> Optional[str]:
    """\'06.02\' / \'06.02~06.03\' → \'YYYYMMDD\' (시작일 기준)."""
    m = re.search(r"(\\d{1,2})\\.(\\d{2})", s)
    if not m:
        return None
    mon, day = int(m.group(1)), int(m.group(2))
    return f"{year}{mon:02d}{day:02d}"'''

NEW_DATE_38 = '''\
def _parse_date_38(s: str, year: int) -> Optional[str]:
    """날짜 파싱: \'YYYY.MM.DD\', \'MM.DD\', \'YYYY.MM.DD~MM.DD\' 등 지원."""
    # 우선 YYYY.MM.DD 전체 형식 탐색 (2026.06.18 같은 패턴)
    m = re.search(r"(\\d{4})\\.(\\d{1,2})\\.(\\d{2})", s)
    if m:
        y, mon, day = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return f"{y}{mon:02d}{day:02d}"
    # MM.DD 형식 fallback (월 범위 검증으로 연도 오파싱 방지)
    m = re.search(r"\\b(\\d{1,2})\\.(\\d{2})\\b", s)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return f"{year}{mon:02d}{day:02d}"
    return None'''


# ── 패치 3: _parse_date_38_end — YYYY.MM.DD~MM.DD 종료일 추출 ───────────
OLD_DATE_38_END = '''\
def _parse_date_38_end(s: str, year: int) -> Optional[str]:
    """\'06.02~06.03\' → 종료일 \'YYYYMMDD\'."""
    parts = s.split("~")
    if len(parts) < 2:
        return _parse_date_38(s, year)
    return _parse_date_38(parts[1].strip(), year)'''

NEW_DATE_38_END = '''\
def _parse_date_38_end(s: str, year: int) -> Optional[str]:
    """\'YYYY.MM.DD~MM.DD\' 또는 \'MM.DD~MM.DD\' 에서 종료일 추출."""
    if "~" not in s:
        return _parse_date_38(s, year)
    start_str, end_str = [p.strip() for p in s.split("~", 1)]
    # 시작일에서 연도 추출 (종료일이 MM.DD만 있을 경우 사용)
    m_year = re.search(r"(\\d{4})", start_str)
    base_year = int(m_year.group(1)) if m_year else year
    return _parse_date_38(end_str, base_year)'''


# ── 패치 4: _parse_38_html — 컬럼 재매핑 ────────────────────────────────
OLD_PARSE_38 = '''\
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 6:
            continue
        texts = [_clean(c) for c in cells]

        # 종목명 (링크 텍스트)
        corp_name = texts[0]
        if not corp_name or corp_name in ("종목명", "회사명"):
            continue  # 헤더 행 스킵

        # 공모가 범위 (col1)
        band_raw = texts[1] if len(texts) > 1 else ""
        band_nums = re.findall(r"[\\d,]+", band_raw)
        band_low = float(band_nums[0].replace(",", "")) if len(band_nums) >= 1 else None
        band_high = float(band_nums[-1].replace(",", "")) if len(band_nums) >= 2 else band_low

        # 확정공모가 (col2)
        final_raw = texts[2] if len(texts) > 2 else ""
        final_price = _parse_price(final_raw)

        # 청약일 (col3)
        sub_raw = texts[3] if len(texts) > 3 else ""
        sub_start = _parse_date_38(sub_raw, year)
        sub_end   = _parse_date_38_end(sub_raw, year)

        # 상장일 (col5)
        listing_raw = texts[5] if len(texts) > 5 else ""
        listing_date = _parse_date_38(listing_raw, year)

        # 주관사 (col6)
        underwriter = texts[6].split("/")[0].strip() if len(texts) > 6 else None'''

NEW_PARSE_38 = '''\
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 5:
            continue
        texts = [_clean(c) for c in cells]

        # 종목명 (col0, 링크 텍스트, &nbsp; 제거 후)
        corp_name = texts[0]
        if not corp_name or corp_name in ("종목명", "회사명"):
            continue  # 헤더 행 스킵

        # ── 실제 38.co.kr 컬럼 구조 (2026.05 확인) ──────────────────
        # col0: 종목명        col1: 청약기간 (YYYY.MM.DD~MM.DD)
        # col2: 확정공모가    col3: 공모가범위 (숫자~숫자)
        # col4: 기타          col5: 주관사 (쉼표 구분)
        # col6: 분석버튼 (skip)
        # ──────────────────────────────────────────────────────────────

        # 청약기간 (col1): "2026.06.18~06.19"
        sub_raw = texts[1] if len(texts) > 1 else ""
        sub_start = _parse_date_38(sub_raw, year)
        sub_end   = _parse_date_38_end(sub_raw, year)

        # 확정공모가 (col2): "-" or "22,000"
        final_raw = texts[2] if len(texts) > 2 else ""
        final_price = _parse_price(final_raw)

        # 공모가 범위 (col3): "22,000~27,000"
        band_raw = texts[3] if len(texts) > 3 else ""
        band_nums = re.findall(r"[\\d,]+", band_raw)
        band_low = float(band_nums[0].replace(",", "")) if len(band_nums) >= 1 else None
        band_high = float(band_nums[-1].replace(",", "")) if len(band_nums) >= 2 else band_low

        # 상장일: 38 목록 페이지에는 없음 → None (개별 페이지에서만 확인 가능)
        listing_date = None

        # 주관사 (col5): "미래에셋증권,이모아증권"
        underwriter = texts[5].split(",")[0].strip() if len(texts) > 5 else None'''


PATCHES = [
    ("_clean 엔티티 처리",    OLD_CLEAN,        NEW_CLEAN),
    ("_parse_date_38 개선",   OLD_DATE_38,      NEW_DATE_38),
    ("_parse_date_38_end 개선", OLD_DATE_38_END, NEW_DATE_38_END),
    ("_parse_38_html 컬럼 재매핑", OLD_PARSE_38, NEW_PARSE_38),
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
        # 역슬래시 이스케이프 정리 (패치 소스가 파이썬 스트링이라 \\n → \n)
        old_clean = old.replace("\\\\", "\\")
        new_clean = new.replace("\\\\", "\\")
        if old_clean in patched:
            patched = patched.replace(old_clean, new_clean, 1)
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
        print("\n⚠️  변경 없음 — 모두 이미 적용됐거나 불일치.")
        return

    if not apply:
        print(f"\n🔍 dry-run: 실제 적용하려면 --apply 옵션을 추가하세요.")
        # 변경 미리보기 (처음 다른 줄 5개)
        old_lines = src.splitlines()
        new_lines = patched.splitlines()
        diff_count = 0
        for i, (a, b) in enumerate(zip(old_lines, new_lines)):
            if a != b and diff_count < 5:
                print(f"  L{i+1}  - {a[:80]}")
                print(f"       + {b[:80]}")
                diff_count += 1
        if diff_count == 0:
            extra = len(new_lines) - len(old_lines)
            print(f"  (라인 수 변화: {extra:+d})")
        return

    # 백업 후 적용
    backup = TARGET.with_suffix(".py.bak")
    shutil.copy2(TARGET, backup)
    print(f"\n  백업: {backup.name}")
    TARGET.write_text(patched, encoding="utf-8")
    print(f"  ✅ 패치 적용 완료: {TARGET.name}")
    print(f"\n검증:")
    print(f"  python scripts/verify_parsers.py --source 38")


if __name__ == "__main__":
    main()
