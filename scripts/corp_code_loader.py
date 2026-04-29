#!/usr/bin/env python3
"""
DART corp_code 매핑 유틸 (ticker ↔ corp_code).

DART는 8자리 corp_code를 사용함. 주식 종목코드(005930 등)와 다름.
한국거래소 ticker → DART corp_code 변환이 모든 DART 호출의 선행 작업.

corpCode.xml은 거의 변하지 않으므로 24시간 캐싱.

사용:
    python corp_code_loader.py --ticker 005930        # 단일 조회
    python corp_code_loader.py --refresh              # 캐시 강제 갱신
"""
from __future__ import annotations
import argparse
import io
import json
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).resolve().parent.parent  # ai-agent/
ENV_PATH = ROOT / ".env"
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "corp_codes.json"
CACHE_TTL = 86400  # 24시간


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_dart_key() -> str:
    env = load_env(ENV_PATH)
    key = env.get("DART_API_KEY", "").strip()
    if not key:
        sys.exit(f"❌ DART_API_KEY가 {ENV_PATH}에 없음")
    return key


def fetch_corp_codes(key: str) -> dict[str, dict]:
    """
    DART corpCode.xml 다운로드 → 압축 해제 → 파싱
    반환: {stock_code: {"corp_code": ..., "corp_name": ..., "modify_date": ...}}
    """
    url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={key}"
    print(f"DART corp_code 다운로드 중...", file=sys.stderr)
    try:
        with urlopen(url, timeout=60) as r:
            zip_data = r.read()
    except (URLError, HTTPError) as e:
        sys.exit(f"❌ 다운로드 실패: {e}")

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        xml_text = zf.read("CORPCODE.xml").decode("utf-8")

    root = ET.fromstring(xml_text)
    mapping: dict[str, dict] = {}
    total = 0
    listed = 0
    for entry in root.findall("list"):
        total += 1
        stock_code = (entry.findtext("stock_code") or "").strip()
        # 상장주 필터 (stock_code가 비공백일 때만)
        if not stock_code or stock_code in ("", " "):
            continue
        listed += 1
        mapping[stock_code] = {
            "corp_code": (entry.findtext("corp_code") or "").strip(),
            "corp_name": (entry.findtext("corp_name") or "").strip(),
            "modify_date": (entry.findtext("modify_date") or "").strip(),
        }
    print(f"✅ 전체 {total}개 중 상장사 {listed}개 매핑", file=sys.stderr)
    return mapping


def get_corp_code_map(refresh: bool = False) -> dict[str, dict]:
    """캐시 우선. TTL 만료 시 자동 재다운로드."""
    if (
        not refresh
        and CACHE_FILE.exists()
        and (time.time() - CACHE_FILE.stat().st_mtime) < CACHE_TTL
    ):
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))

    key = get_dart_key()
    mapping = fetch_corp_codes(key)
    CACHE_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    return mapping


def lookup(ticker: str, refresh: bool = False) -> dict | None:
    """단일 ticker 조회"""
    ticker = ticker.strip().zfill(6)  # "5930" → "005930"
    mapping = get_corp_code_map(refresh=refresh)
    return mapping.get(ticker)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", help="종목코드 (예: 005930)")
    ap.add_argument("--refresh", action="store_true", help="캐시 강제 갱신")
    args = ap.parse_args()

    if args.refresh:
        get_corp_code_map(refresh=True)
        print("✅ 캐시 갱신 완료")

    if args.ticker:
        info = lookup(args.ticker)
        if not info:
            print(f"❌ 종목코드 {args.ticker}를 DART에서 찾을 수 없음")
            sys.exit(1)
        print(f"종목코드:    {args.ticker.zfill(6)}")
        print(f"기업명:      {info['corp_name']}")
        print(f"corp_code:   {info['corp_code']}")
        print(f"수정일:      {info['modify_date']}")
    elif not args.refresh:
        print("사용법: --ticker 005930  또는  --refresh")
        sys.exit(1)


if __name__ == "__main__":
    main()
