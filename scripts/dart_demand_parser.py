#!/usr/bin/env python3
"""
DART IPO 수요예측 공시 파싱 → IPO봇 매력지수 입력.

추출 목표:
- 기관 수요예측 경쟁률
- 의무보유 확약비율
- 공모가 상단 초과 여부
- 공모가 밴드 / 확정 공모가
- 시가총액 (확정 기준)

⚠️ KIND API는 정형 데이터 미제공 → DART 텍스트 파싱 필수.
KIND/38커뮤니케이션은 일정 수집용. 수요예측 결과는 DART에서.

v1 동작:
- 최근 N일간 IPO 관련 공시 목록 수집
- 각 공시의 첨부 문서 다운로드 (XML)
- 텍스트에서 정규식으로 핵심 수치 추출 시도
- 추출 성공률 + raw 텍스트 일부를 검증용으로 출력/저장

사용:
    # 최근 30일 IPO 공시 목록 + 자동 파싱 시도
    python dart_demand_parser.py --days 30

    # 특정 공시 1건 분석 (rcept_no)
    python dart_demand_parser.py --rcept 20240612000123
"""
from __future__ import annotations
import argparse
import csv
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corp_code_loader import get_dart_key, ROOT  # noqa: E402

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SAMPLES_DIR = DATA_DIR / "ipo_samples"
SAMPLES_DIR.mkdir(exist_ok=True)


# DART 공시 유형
# pblntf_ty="C" 발행공시, pblntf_detail_ty="C001" 증권신고서
IPO_FILING_TYPES = ["C001", "C002", "C003", "C004", "C005"]


def list_ipo_filings(start: str, end: str, page: int = 1) -> list[dict]:
    """기간 내 IPO 관련 공시 목록"""
    key = get_dart_key()
    results: list[dict] = []
    for ty in IPO_FILING_TYPES:
        params = {
            "crtfc_key": key,
            "bgn_de": start,
            "end_de": end,
            "pblntf_detail_ty": ty,
            "page_count": "100",
            "page_no": str(page),
        }
        url = "https://opendart.fss.or.kr/api/list.json?" + urlencode(params)
        try:
            with urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (URLError, HTTPError) as e:
            print(f"⚠️ 목록 조회 실패 ({ty}): {e}", file=sys.stderr)
            continue
        if data.get("status") != "000":
            continue
        for item in data.get("list", []):
            item["pblntf_detail_ty"] = ty
            results.append(item)
    return results


def fetch_document_text(rcept_no: str) -> str | None:
    """공시 본문 (XML) 다운로드 → 텍스트 추출"""
    key = get_dart_key()
    url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={key}&rcept_no={rcept_no}"
    try:
        with urlopen(url, timeout=30) as r:
            zip_data = r.read()
    except (URLError, HTTPError) as e:
        print(f"⚠️ 문서 다운로드 실패 {rcept_no}: {e}", file=sys.stderr)
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            # 첫 번째 XML 파일 사용
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not xml_names:
                return None
            raw = zf.read(xml_names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        # XML 단일 응답일 가능
        try:
            return zip_data.decode("utf-8", errors="replace")
        except Exception:
            return None

    # XML 태그 제거하고 텍스트만 추출
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text)
    return text


# ─── 정규식 추출 패턴 (v1 — 샘플 검증 후 정밀화 필요) ──────────────

PATTERNS = {
    "competition_rate": [
        r"수요예측\s*(?:경쟁률|결과)[^\d]*(\d{1,4}(?:[.,]\d+)?)\s*[:：]\s*1",
        r"기관\s*경쟁률[^\d]*(\d{1,4}(?:[.,]\d+)?)\s*[:：]\s*1",
        r"경쟁률[^\d]{0,30}?(\d{1,4}(?:[.,]\d+)?)\s*대\s*1",
    ],
    "lockup_ratio": [
        r"의무\s*보유\s*확약\s*비율[^\d]*(\d{1,3}(?:[.,]\d+)?)\s*%",
        r"확약\s*비율[^\d]{0,30}?(\d{1,3}(?:[.,]\d+)?)\s*%",
    ],
    "offer_band_high": [
        r"공모\s*희망\s*가격[^\d]*(\d{1,3}(?:,\d{3})*)\s*~\s*(\d{1,3}(?:,\d{3})*)\s*원",
        r"공모가\s*밴드[^\d]*(\d{1,3}(?:,\d{3})*)\s*~\s*(\d{1,3}(?:,\d{3})*)\s*원",
    ],
    "final_price": [
        r"확정\s*공모\s*가격[^\d]*(\d{1,3}(?:,\d{3})*)\s*원",
        r"공모가\s*확정[^\d]{0,20}?(\d{1,3}(?:,\d{3})*)\s*원",
    ],
}


def _to_num(s: str) -> float:
    return float(s.replace(",", ""))


def extract_ipo_metrics(text: str) -> dict:
    """텍스트에서 IPO 수치 추출. 실패한 필드는 None."""
    out = {
        "competition_rate": None,
        "lockup_ratio": None,
        "offer_band_low": None,
        "offer_band_high": None,
        "final_price": None,
        "above_band": None,  # bool
    }

    for pat in PATTERNS["competition_rate"]:
        m = re.search(pat, text)
        if m:
            out["competition_rate"] = _to_num(m.group(1))
            break

    for pat in PATTERNS["lockup_ratio"]:
        m = re.search(pat, text)
        if m:
            out["lockup_ratio"] = _to_num(m.group(1))
            break

    for pat in PATTERNS["offer_band_high"]:
        m = re.search(pat, text)
        if m:
            out["offer_band_low"] = _to_num(m.group(1))
            out["offer_band_high"] = _to_num(m.group(2))
            break

    for pat in PATTERNS["final_price"]:
        m = re.search(pat, text)
        if m:
            out["final_price"] = _to_num(m.group(1))
            break

    if out["final_price"] and out["offer_band_high"]:
        out["above_band"] = out["final_price"] > out["offer_band_high"]

    return out


def parse_one(rcept_no: str, save_text: bool = True) -> dict:
    """단일 공시 처리"""
    text = fetch_document_text(rcept_no)
    if not text:
        return {"rcept_no": rcept_no, "error": "문서 다운로드 실패"}

    if save_text:
        sample_path = SAMPLES_DIR / f"{rcept_no}.txt"
        sample_path.write_text(text[:50000], encoding="utf-8")  # 50KB 미리보기

    metrics = extract_ipo_metrics(text)
    return {"rcept_no": rcept_no, **metrics, "text_len": len(text)}


def run_recent(days: int = 30) -> None:
    """최근 N일 IPO 공시 일괄 시도"""
    end = date.today()
    start = end - timedelta(days=days)

    print(f"기간: {start} ~ {end}")
    filings = list_ipo_filings(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    # 중복 제거
    seen = set()
    uniq = []
    for f in filings:
        rno = f.get("rcept_no")
        if rno and rno not in seen:
            seen.add(rno)
            uniq.append(f)
    print(f"공시 {len(uniq)}건 발견 (중복 제거 후)")

    results = []
    for i, f in enumerate(uniq[:20], 1):  # v1: 최대 20건
        rno = f["rcept_no"]
        corp = f.get("corp_name", "?")
        rep_nm = f.get("report_nm", "?")
        print(f"[{i}/{min(len(uniq),20)}] {corp} — {rep_nm}")
        r = parse_one(rno)
        r["corp_name"] = corp
        r["report_nm"] = rep_nm
        r["rcept_dt"] = f.get("rcept_dt", "")
        results.append(r)

    # CSV 저장
    csv_path = DATA_DIR / "ipo_demand_validation.csv"
    fieldnames = [
        "rcept_dt", "corp_name", "report_nm", "rcept_no",
        "competition_rate", "lockup_ratio",
        "offer_band_low", "offer_band_high", "final_price", "above_band",
        "text_len", "error",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    # 추출 통계
    print("\n" + "=" * 60)
    print(f"총 {len(results)}건 처리")
    fields = ["competition_rate", "lockup_ratio", "offer_band_high", "final_price"]
    for fld in fields:
        n = sum(1 for r in results if r.get(fld) is not None)
        print(f"  {fld:25s}: {n}/{len(results)} 추출 성공")
    print(f"\nCSV: {csv_path}")
    print(f"본문 샘플: {SAMPLES_DIR}/")
    print("\n👉 다음 단계: 추출 실패 케이스의 본문 샘플 확인 → 정규식 패턴 보강")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="최근 N일 (기본 30)")
    ap.add_argument("--rcept", help="특정 공시 rcept_no 단건 분석")
    args = ap.parse_args()

    if args.rcept:
        r = parse_one(args.rcept)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        run_recent(args.days)


if __name__ == "__main__":
    main()
