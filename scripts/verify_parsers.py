#!/usr/bin/env python3
"""
verify_parsers.py — IPO봇 38/KIND 파서 실사 검증 도구 (v3.26)

사용법:
  # Step 1: HTML 수집 (Mac 터미널에서 실행)
  python scripts/ipo_bot.py debug-html --source 38
  python scripts/ipo_bot.py debug-html --source kind

  # 위가 안 되면 curl로 직접 저장
  mkdir -p data/ipo_samples
  curl -k -o data/ipo_samples/38_raw.html "https://www.38.co.kr/html/fund/index.htm?o=k"
  curl -k -o data/ipo_samples/kind_raw.html \
    "https://kind.krx.co.kr/listinginfo/iposummary.do?method=searchIpoSummary&forward=iposummary_info"

  # Step 2: 파서 검증
  cd ~/울트론/ai-agent
  python scripts/verify_parsers.py
  python scripts/verify_parsers.py --source 38
  python scripts/verify_parsers.py --source kind
  python scripts/verify_parsers.py --source scan   # 통합 파이프라인 테스트
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

DATA_DIR   = PROJECT / "data"
SAMPLE_DIR = DATA_DIR / "ipo_samples"

from ipo_bot import (
    _parse_38_html,
    _parse_kind_html,
    _clean,
    compute_attraction_score,
    format_result,
)
from dataclasses import asdict


# ─── 38커뮤니케이션 검증 ─────────────────────────────

def verify_38(verbose: bool = True) -> bool:
    html_path = SAMPLE_DIR / "38_raw.html"
    if not html_path.exists():
        print("❌ 38_raw.html 없음. 먼저 수집하세요:")
        print("   python scripts/ipo_bot.py debug-html --source 38")
        print("   또는: mkdir -p data/ipo_samples && curl -k -o data/ipo_samples/38_raw.html "
              "'https://www.38.co.kr/html/fund/index.htm?o=k'")
        return False

    raw = html_path.read_text(encoding="utf-8", errors="replace")
    print(f"\n{'='*60}")
    print(f"[38커뮤니케이션] 파일 크기: {len(raw):,} bytes")

    tr_count    = len(re.findall(r"<tr",    raw, re.IGNORECASE))
    td_count    = len(re.findall(r"<td",    raw, re.IGNORECASE))
    table_count = len(re.findall(r"<table", raw, re.IGNORECASE))
    print(f"  <table> {table_count}개 / <tr> {tr_count}개 / <td> {td_count}개")

    if tr_count == 0:
        print("  ⚠️  <tr> 태그 없음. JS 동적 렌더링 가능성.")
        _show_raw_snippet(raw)
        return False

    items = _parse_38_html(raw)
    print(f"\n  → 파서 결과: {len(items)}건")

    if not items:
        print("  ❌ 파싱 결과 없음! HTML 구조 분석:")
        _analyze_table_structure(raw, "38")
        return False

    print(f"  ✅ {len(items)}건 파싱 성공\n")
    ok_count = 0
    for i, item in enumerate(items, 1):
        has_name  = bool(item.corp_name)
        has_date  = bool(item.sub_start)
        status = "✅" if (has_name and has_date) else "⚠️ "
        if has_name and has_date:
            ok_count += 1
        if verbose:
            print(f"  {status} [{i:02d}] {item.corp_name or '(이름없음)'}")
            print(f"        공모가 {item.band_low}~{item.band_high} / 확정 {item.final_price}")
            print(f"        청약 {item.sub_start}~{item.sub_end} / 상장 {item.listing_date}")
            print(f"        주관사: {item.underwriter}")

    print(f"\n  최종: {ok_count}/{len(items)}건 정상 (기업명+청약일 확인)")

    if items:
        result = compute_attraction_score(items[0])
        print(f"  [매력도] {items[0].corp_name}: "
              f"등급={result.grade}, 점수={result.total_score}, "
              f"확정요소={result.confirmed_factors}/5")

    return ok_count > 0


# ─── KIND 검증 ───────────────────────────────────────

def verify_kind(verbose: bool = True) -> bool:
    html_path = SAMPLE_DIR / "kind_raw.html"
    if not html_path.exists():
        print("❌ kind_raw.html 없음. 먼저 수집하세요:")
        print("   python scripts/ipo_bot.py debug-html --source kind")
        print("   또는: mkdir -p data/ipo_samples && curl -k -o data/ipo_samples/kind_raw.html \\")
        print("     'https://kind.krx.co.kr/listinginfo/iposummary.do"
              "?method=searchIpoSummary&forward=iposummary_info'")
        return False

    # KIND는 EUC-KR 혹은 UTF-8
    raw = None
    used_enc = None
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            raw = html_path.read_text(encoding=enc, errors="strict")
            used_enc = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if raw is None:
        raw = html_path.read_bytes().decode("utf-8", errors="replace")
        used_enc = "utf-8(fallback)"

    print(f"\n{'='*60}")
    print(f"[KIND] 파일 크기: {len(raw):,} bytes / 인코딩: {used_enc}")

    tr_count    = len(re.findall(r"<tr",    raw, re.IGNORECASE))
    td_count    = len(re.findall(r"<td",    raw, re.IGNORECASE))
    table_count = len(re.findall(r"<table", raw, re.IGNORECASE))
    print(f"  <table> {table_count}개 / <tr> {tr_count}개 / <td> {td_count}개")

    # HTTP 에러 조기 감지
    if re.search(r"404|Not Found|페이지를 찾을", raw[:800], re.IGNORECASE):
        print("  ❌ 404 응답 감지. URL 또는 POST 파라미터 문제.")
        _show_raw_snippet(raw, 400)
        print("\n  → 해결책: 브라우저 Network 탭에서 실제 XHR URL + payload 캡처")
        return False

    if tr_count == 0:
        print("  ⚠️  <tr> 없음 — JS 동적 렌더링 또는 빈 응답 가능성.")
        _show_raw_snippet(raw)
        return False

    items = _parse_kind_html(raw)
    print(f"\n  → 파서 결과: {len(items)}건")

    if not items:
        print("  ❌ 파싱 결과 없음! HTML 구조 분석:")
        _analyze_table_structure(raw, "kind")
        return False

    print(f"  ✅ {len(items)}건 파싱 성공\n")
    ok_count = 0
    for i, item in enumerate(items, 1):
        has_name = bool(item.corp_name)
        has_date = bool(item.listing_date or item.sub_start)
        status = "✅" if (has_name and has_date) else "⚠️ "
        if has_name and has_date:
            ok_count += 1
        if verbose:
            print(f"  {status} [{i:02d}] {item.corp_name or '(이름없음)'}")
            print(f"        공모가 {item.band_low}~{item.band_high} / 확정 {item.final_price}")
            print(f"        시총 {item.market_cap}억 / 주관사 {item.underwriter}")
            print(f"        청약 {item.sub_start}~{item.sub_end} / 상장 {item.listing_date}")

    print(f"\n  최종: {ok_count}/{len(items)}건 정상 (기업명+날짜 확인)")

    if items:
        result = compute_attraction_score(items[0])
        print(f"  [매력도] {items[0].corp_name}: "
              f"등급={result.grade}, 점수={result.total_score}, "
              f"확정요소={result.confirmed_factors}/5")

    return ok_count > 0


# ─── 통합 스캔 테스트 ────────────────────────────────

def verify_scan_from_file() -> None:
    """저장된 HTML로 scan_upcoming과 동일한 파이프라인 검증."""
    print(f"\n{'='*60}")
    print("[통합 파이프라인 테스트]")
    items = []

    kind_path = SAMPLE_DIR / "kind_raw.html"
    if kind_path.exists():
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                raw = kind_path.read_text(encoding=enc, errors="strict")
                parsed = _parse_kind_html(raw)
                if parsed:
                    items += parsed
                    print(f"  KIND 로드: {len(parsed)}건")
                break
            except (UnicodeDecodeError, LookupError):
                continue

    if not items:
        path38 = SAMPLE_DIR / "38_raw.html"
        if path38.exists():
            raw = path38.read_text(encoding="utf-8", errors="replace")
            parsed = _parse_38_html(raw)
            items = parsed
            print(f"  38커뮤니케이션 fallback: {len(parsed)}건")

    if not items:
        print("  ❌ 수집된 종목 없음 — HTML 파일 먼저 저장하세요")
        return

    results = []
    for item in items:
        score_result = compute_attraction_score(item)
        row = asdict(item)
        row.update(asdict(score_result))
        results.append(row)

    results.sort(key=lambda r: r.get("total_score") or -1, reverse=True)
    print(format_result(results[:5]))


# ─── 헬퍼 ────────────────────────────────────────────

def _analyze_table_structure(raw: str, source: str) -> None:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.DOTALL | re.IGNORECASE)
    if not rows:
        print("  <tr> 없음")
        _show_raw_snippet(raw)
        return
    print(f"  전체 {len(rows)}행 중 앞 5행 컬럼 미리보기:")
    for i, row in enumerate(rows[:5], 1):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        texts = [_clean(c)[:25] for c in cells]
        if texts:
            print(f"  row{i} ({len(cells)}열): {texts[:7]}")
    if source == "kind":
        print("\n  → KIND가 XHR API면 브라우저 Network 탭에서 실제 요청 URL + payload 확인 필요")
    print(f"  → 컬럼 순서 확인 후 _parse_{source}_html() 수정")


def _show_raw_snippet(raw: str, chars: int = 600) -> None:
    snippet = re.sub(r"\s+", " ", raw[:chars])
    print(f"\n  [HTML 앞부분 {chars}자]")
    print(f"  {snippet}")


# ─── CLI ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="IPO봇 파서 실사 검증")
    ap.add_argument(
        "--source", choices=["38", "kind", "both", "scan"],
        default="both",
        help="검증 대상 (기본: both)",
    )
    ap.add_argument("--quiet", action="store_true", help="항목별 상세 출력 생략")
    args = ap.parse_args()

    verbose = not args.quiet

    if args.source == "scan":
        verify_scan_from_file()
        return

    ok_38 = ok_kind = None
    if args.source in ("38", "both"):
        ok_38 = verify_38(verbose=verbose)
    if args.source in ("kind", "both"):
        ok_kind = verify_kind(verbose=verbose)

    print(f"\n{'='*60}")
    print("[검증 요약]")
    if ok_38 is not None:
        print(f"  38커뮤니케이션: {'✅ 정상' if ok_38 else '❌ 실패'}")
    if ok_kind is not None:
        print(f"  KIND           : {'✅ 정상' if ok_kind else '❌ 실패'}")

    if args.source == "both":
        if ok_38 and ok_kind:
            print("\n  🎉 양쪽 파서 모두 정상!")
            print("     python scripts/verify_parsers.py --source scan  # 통합 파이프라인 확인")
        elif ok_38:
            print("\n  ⚠️  38만 동작. KIND URL/POST 파라미터 수정 필요.")
        elif ok_kind:
            print("\n  ⚠️  KIND만 동작. 38 SSL 설정 확인 필요.")
        else:
            print("\n  ❌ 두 파서 모두 실패. HTML 재수집 또는 파서 수정 필요.")


if __name__ == "__main__":
    main()
