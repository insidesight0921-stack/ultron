#!/usr/bin/env python3
"""
DART 분기/연간 재무제표 → 콴텍봇 팩터 추출.

추출 항목:
- ROE (당기순이익 / 자본총계)
- 부채비율 (부채총계 / 자본총계 × 100)
- 영업이익률 (영업이익 / 매출액 × 100)

사용:
    # 단일 종목 최근 분기
    python dart_finance_parser.py --ticker 005930

    # 특정 연도/분기 지정
    python dart_finance_parser.py --ticker 005930 --year 2024 --quarter 4

    # 검증용 샘플 10개 일괄 (계획서 0단계 권장)
    python dart_finance_parser.py --validate
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# 같은 폴더의 corp_code_loader 사용
sys.path.insert(0, str(Path(__file__).resolve().parent))
from corp_code_loader import lookup, get_dart_key, ROOT  # noqa: E402
from storage_paths import PATHS  # noqa: E402

# 분기 → DART reprt_code
REPRT_CODE = {
    1: "11013",  # 1분기 (3월)
    2: "11012",  # 반기 (6월, 누적)
    3: "11014",  # 3분기 (9월, 누적)
    4: "11011",  # 사업보고서 (12월, 연간)
}

# 검증용 샘플 10개 (계획서 0단계 — 실제 분포 확인 후 자동화)
VALIDATION_TICKERS = [
    ("005930", "삼성전자"),       # 대형 IT
    ("000660", "SK하이닉스"),     # 반도체
    ("207940", "삼성바이오로직스"), # 바이오
    ("373220", "LG에너지솔루션"),  # 2차전지
    ("105560", "KB금융"),         # 금융
    ("055550", "신한지주"),        # 금융
    ("005380", "현대차"),          # 자동차
    ("035420", "NAVER"),          # IT 플랫폼
    ("035720", "카카오"),          # IT 플랫폼
    ("068270", "셀트리온"),        # 바이오
]

def get_financials(corp_code: str, year: int, quarter: int, fs_div: str = "CFS") -> list | None:
    """fnlttSinglAcntAll API 호출. CFS(연결) 우선, 없으면 OFS(별도)."""
    key = get_dart_key()
    params = {
        "crtfc_key": key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": REPRT_CODE[quarter],
        "fs_div": fs_div,
    }
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?" + urlencode(params)
    try:
        with urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (URLError, HTTPError) as e:
        return None

    if data.get("status") != "000":
        # 연결 없으면 별도 시도
        if fs_div == "CFS":
            return get_financials(corp_code, year, quarter, "OFS")
        return None
    return data.get("list", [])


def _safe_int(s: str) -> int:
    """DART는 천 단위 콤마 + 음수는 -123 또는 (123) 형태"""
    if not s:
        return 0
    s = s.replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return int(float(s))
    except ValueError:
        return 0


def parse_factors(items: list) -> tuple[dict, dict]:
    """
    주요 계정 추출 + 팩터 계산.

    핵심 규칙:
    1. sj_div 필터 — 자산/부채/자본은 BS에서만, 매출/이익은 IS/CIS에서만
       (SCE 자본변동표에 동명 항목 있어서 필터 없으면 잘못 잡힘)
    2. 자산/부채/자본 총계 — EXACT MATCH (substring 금지)
    3. 매출/영업수익 — 회사 형태별 우선순위
    """
    # sj_div 별로 분리
    bs = [i for i in items if (i.get("sj_div") or "").strip() == "BS"]
    is_ = [i for i in items if (i.get("sj_div") or "").strip() in ("IS", "CIS")]

    accounts = {}

    # ─── 재무상태표(BS): 자산/부채/자본 총계 ─────────────
    for item in bs:
        nm = (item.get("account_nm") or "").strip()
        amt = _safe_int(item.get("thstrm_amount", ""))
        if nm == "자산총계":
            accounts["total_assets"] = amt
        elif nm == "부채총계":
            accounts["total_liab"] = amt
        elif nm == "자본총계":
            accounts["total_equity"] = amt

    # ─── 손익계산서(IS/CIS): 매출/영업이익/순이익 ─────────
    # 변형 표기 대응: '영업이익(손실)', '당기순이익(손실)' 등은 손실 가능성 표시일 뿐
    # 실제 수치 부호로 손익 판단 (양수=이익, 음수=손실)
    OP_INCOME_NAMES = {
        "영업이익", "영업이익(손실)", "영업손실(이익)",
        "연결영업이익", "연결영업이익(손실)",
    }
    NET_INCOME_NAMES = {
        "당기순이익", "분기순이익", "반기순이익",
        "당기순이익(손실)", "분기순이익(손실)", "반기순이익(손실)",
        "연결당기순이익", "연결분기순이익", "연결반기순이익",
        "연결당기순이익(손실)",
    }

    for item in is_:
        nm = (item.get("account_nm") or "").strip()
        amt = _safe_int(item.get("thstrm_amount", ""))
        if nm in OP_INCOME_NAMES:
            accounts["op_income"] = amt
        elif nm == "영업손실":  # exact name으로 손실 표기된 경우만 부호 보정
            accounts["op_income"] = -abs(amt)
        elif nm in NET_INCOME_NAMES:
            accounts.setdefault("net_income", amt)
        elif "지배기업" in nm and any(k in nm for k in ("당기순이익", "분기순이익", "반기순이익")):
            accounts["net_income"] = amt  # 지배기업 귀속이 더 정확

    # ─── 매출액 우선순위 (IS/CIS 안에서만) ────────────────
    revenue_priority = [
        "매출액",
        "수익(매출액)",
        "영업수익",
        "영업총수익",
        "이자수익",
        "보험료수익",
    ]
    revenue_found_at = None
    for nm_target in revenue_priority:
        for item in is_:
            nm = (item.get("account_nm") or "").strip()
            if nm == nm_target:
                accounts["revenue"] = _safe_int(item.get("thstrm_amount", ""))
                revenue_found_at = nm_target
                break
        if revenue_found_at:
            break

    accounts["_revenue_source"] = revenue_found_at

    # 팩터 계산
    factors = {}
    if accounts.get("net_income") is not None and accounts.get("total_equity"):
        factors["ROE"] = accounts["net_income"] / accounts["total_equity"] * 100
    if accounts.get("total_liab") is not None and accounts.get("total_equity"):
        factors["debt_ratio"] = accounts["total_liab"] / accounts["total_equity"] * 100
    if accounts.get("op_income") is not None and accounts.get("revenue"):
        factors["op_margin"] = accounts["op_income"] / accounts["revenue"] * 100

    return accounts, factors


def fmt_won(amt: int) -> str:
    """원 단위 → 조/억 표기"""
    if amt is None:
        return "—"
    sign = "-" if amt < 0 else ""
    a = abs(amt)
    if a >= 1_000_000_000_000:
        return f"{sign}{a/1_000_000_000_000:,.2f}조"
    if a >= 100_000_000:
        return f"{sign}{a/100_000_000:,.0f}억"
    return f"{sign}{a:,}"


def fetch_one(ticker: str, name: str | None = None, year: int | None = None, quarter: int | None = None) -> dict:
    """단일 종목 최신 분기 (or 지정 분기) 재무"""
    info = lookup(ticker)
    if not info:
        return {"ticker": ticker, "error": "corp_code 매핑 실패"}

    corp_code = info["corp_code"]
    name = name or info["corp_name"]

    # 분기 미지정 시: 가장 최근 데이터 있는 분기를 자동 탐색
    if not year or not quarter:
        today = date.today()
        # 최근부터 거꾸로 탐색
        candidates = []
        y = today.year
        for q in [4, 3, 2, 1]:
            candidates.append((y, q))
        for q in [4, 3, 2, 1]:
            candidates.append((y - 1, q))

        for cy, cq in candidates:
            items = get_financials(corp_code, cy, cq)
            if items:
                year, quarter = cy, cq
                break
        else:
            return {"ticker": ticker, "name": name, "error": "최근 4개 분기 데이터 모두 없음"}
    else:
        items = get_financials(corp_code, year, quarter)
        if not items:
            return {"ticker": ticker, "name": name, "error": f"{year}-Q{quarter} 데이터 없음"}

    accounts, factors = parse_factors(items)
    return {
        "ticker": ticker,
        "name": name,
        "corp_code": corp_code,
        "year": year,
        "quarter": quarter,
        **accounts,
        **factors,
    }


def print_one(r: dict) -> None:
    if "error" in r:
        print(f"❌ {r['ticker']} ({r.get('name','?')}): {r['error']}")
        return
    src = r.get("_revenue_source") or "?"
    print(f"\n[{r['ticker']}] {r['name']} — {r['year']}년 Q{r['quarter']}")
    print(f"  매출액:     {fmt_won(r.get('revenue'))}  [{src}]")
    print(f"  영업이익:   {fmt_won(r.get('op_income'))}")
    print(f"  당기순이익: {fmt_won(r.get('net_income'))}")
    print(f"  자산총계:   {fmt_won(r.get('total_assets'))}")
    print(f"  부채총계:   {fmt_won(r.get('total_liab'))}")
    print(f"  자본총계:   {fmt_won(r.get('total_equity'))}")

    # 정합성 체크: 자산 ≈ 부채 + 자본 (±0.1% 허용)
    a = r.get("total_assets")
    l = r.get("total_liab")
    e = r.get("total_equity")
    if a and l is not None and e is not None:
        diff = abs(a - (l + e))
        rel = diff / a if a else 0
        if rel > 0.001:
            print(f"  ⚠️ 정합성 경고: 자산({a:,}) ≠ 부채({l:,}) + 자본({e:,}) — 차이 {rel*100:.2f}%")

    print(f"  ─────────────────────────")
    if "ROE" in r:
        print(f"  ROE:        {r['ROE']:.2f}%")
    if "debt_ratio" in r:
        print(f"  부채비율:   {r['debt_ratio']:.2f}%")
    if "op_margin" in r:
        print(f"  영업이익률: {r['op_margin']:.2f}%")


def validate() -> None:
    """샘플 10개 검증 (계획서 0단계 권장)"""
    print("=" * 60)
    print("DART 재무 파싱 검증 — 샘플 10개")
    print("=" * 60)
    results = []
    for ticker, name in VALIDATION_TICKERS:
        r = fetch_one(ticker, name)
        print_one(r)
        results.append(r)

    # CSV 저장
    out_csv = PATHS.dart_finance_validation
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker", "name", "corp_code", "year", "quarter",
        "revenue", "op_income", "net_income",
        "total_assets", "total_liab", "total_equity",
        "ROE", "debt_ratio", "op_margin", "error",
    ]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    success = [r for r in results if "error" not in r]
    print("\n" + "=" * 60)
    print(f"성공: {len(success)}/{len(results)}")
    print(f"CSV 저장: {out_csv}")
    print("=" * 60)
    print("\n👉 다음 단계: 위 수치를 네이버 금융/DART에서 직접 확인해서 일치하는지 검증")


def debug_dump(ticker: str, year: int | None, quarter: int | None) -> None:
    """단일 종목 raw items 덤프 (디버깅용)"""
    info = lookup(ticker)
    if not info:
        print(f"❌ corp_code 매핑 실패: {ticker}")
        return
    corp_code = info["corp_code"]

    if not year or not quarter:
        # 최근 분기 자동 탐색
        today = date.today()
        candidates = [(today.year, q) for q in [4, 3, 2, 1]] + [(today.year - 1, q) for q in [4, 3, 2, 1]]
        for cy, cq in candidates:
            items = get_financials(corp_code, cy, cq)
            if items:
                year, quarter = cy, cq
                break
    else:
        items = get_financials(corp_code, year, quarter)

    if not items:
        print("❌ 데이터 없음")
        return

    print(f"\n[{ticker}] {info['corp_name']} — {year}년 Q{quarter}")
    print(f"총 {len(items)}건")
    print(f"{'sj_div':<8} {'account_nm':<40} {'thstrm_amount':>20}")
    print("-" * 75)
    for item in items:
        sj = (item.get("sj_div") or "").strip()
        nm = (item.get("account_nm") or "").strip()
        amt = item.get("thstrm_amount", "")
        print(f"{sj:<8} {nm:<40} {amt:>20}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", help="종목코드 (예: 005930)")
    ap.add_argument("--year", type=int, help="회계연도 (예: 2024)")
    ap.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], help="분기 (1~4)")
    ap.add_argument("--validate", action="store_true", help="샘플 10개 일괄 검증")
    ap.add_argument("--debug", action="store_true", help="raw items 전체 덤프 (--ticker 필수)")
    args = ap.parse_args()

    if args.debug:
        if not args.ticker:
            ap.error("--debug는 --ticker와 함께 사용")
        debug_dump(args.ticker, args.year, args.quarter)
        return

    if args.validate:
        validate()
        return

    if not args.ticker:
        ap.print_help()
        sys.exit(1)

    r = fetch_one(args.ticker, year=args.year, quarter=args.quarter)
    print_one(r)


if __name__ == "__main__":
    main()
