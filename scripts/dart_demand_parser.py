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
from typing import Optional
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
from storage_paths import PATHS  # noqa: E402

SAMPLES_DIR = PATHS.shareable_samples_dir
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_CHARS = 400_000   # 본문 저장 상한 (실측: 최대 977,411자)


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

# 2026-08-28: **실제 공시 본문 20건으로 다시 맞췄다.** 이전 패턴은 추측이었고
# 밴드 0/20, 경쟁률 0/20, 확약 0/20이었다. 원문을 열어 보니 어긋난 지점이 분명했다.
#
#   - 공시는 "공모희망가격"이 아니라 **"희망공모가액"**을 쓴다(어순도 어미도 다르다).
#     "제시 희망공모가액인 13,000원 ~ 16,000원 중 최저가액인 …"
#   - "확정공모가격"이 아니라 **"확정공모가액"**이다.
#     "협의하여 결정한 확정공모가액인 10,000원 기준"
#   - 물결표는 `~` 말고 전각 `～`·`∼`·`-`도 쓰인다.
#
# 경쟁률·확약은 패턴 문제가 아니었다 — **그 값이 든 문서를 읽고 있지 않았다.**
# 수집한 20건은 전부 수요예측 *전* 문서(증권신고서·투자설명서)라서, 등장하는
# "경쟁률"은 전부 주의사항 문구이거나 일반청약 안내다. 정규식을 아무리 다듬어도
# 없는 값은 나오지 않는다. 아래 패턴은 남겨 두되, 실제 수요예측 결과 문서를
# 확보한 뒤에 검증해야 한다.

_TILDE = r"[~∼～\-]"
_WON = r"(\d{1,3}(?:,\d{3})*)"
# 경쟁률은 천 단위 쉼표와 소수점이 함께 온다("1,159.36 대 1").
# 하나만 허용하던 패턴으로는 그 형태를 놓친다.
_RATE = r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"

PATTERNS = {
    "competition_rate": [
        r"수요예측\s*(?:경쟁률|결과)[^\d]{0,40}?" + _RATE + r"\s*[:：]\s*1",
        r"기관\s*경쟁률[^\d]{0,40}?" + _RATE + r"\s*[:：]\s*1",
        r"경쟁률[^\d]{0,40}?" + _RATE + r"\s*(?:대|:|：)\s*1",
        # 어순이 뒤집힌 형태("… 1,159.36 대 1의 경쟁률"). **실측 미검증**이다 —
        # 확보한 20건에 수요예측 결과 문서가 없어 원문으로 확인하지 못했다.
        # 결과 문서를 얻으면 이 패턴부터 검증한다.
        _RATE + r"\s*(?:대|:|：)\s*1\s*의?\s*경쟁률",
    ],
    "lockup_ratio": [
        r"의무\s*보유\s*확약\s*비율[^\d]{0,40}?" + _RATE + r"\s*%",
        r"확약\s*비율[^\d]{0,30}?" + _RATE + r"\s*%",
    ],
    "offer_band_high": [
        # 실측 표현. 어순이 "희망공모가액"이고, 사이에 "인"·공백 등이 낀다.
        r"희망\s*공모\s*가액[^\d]{0,40}?" + _WON + r"\s*원?\s*" + _TILDE + r"\s*" + _WON + r"\s*원",
        r"공모\s*희망\s*가액[^\d]{0,40}?" + _WON + r"\s*원?\s*" + _TILDE + r"\s*" + _WON + r"\s*원",
        r"공모\s*희망\s*가격[^\d]{0,40}?" + _WON + r"\s*원?\s*" + _TILDE + r"\s*" + _WON + r"\s*원",
        r"공모가\s*밴드[^\d]{0,40}?" + _WON + r"\s*원?\s*" + _TILDE + r"\s*" + _WON + r"\s*원",
    ],
    "final_price": [
        r"확정\s*공모\s*가액[^\d]{0,30}?" + _WON + r"\s*원",
        r"확정\s*공모\s*가격[^\d]{0,30}?" + _WON + r"\s*원",
        # `공모가 확정` 패턴은 뺐다. 실측에서 이 패턴이 잡은 5건 중 3건이
        # **밴드 하단을 확정가로 오인**한 것이었다(브릴스 16,500 / 덕산넵코어스
        # 12,400 / 진코스텍 19,500 — 모두 밴드 하단). 확정가가 잘못 채워지면
        # 단계가 "확정"으로 넘어가고 밴드 하단 확정(2점)이 매겨져,
        # **확정되지도 않은 종목이 최저 점수를 받는다.** 없는 편이 낫다.
    ],
}


def _to_num(s: str) -> float:
    return float(s.replace(",", ""))


# 2026-08-28 실측: 경쟁률은 **문장이 아니라 표**로 실린다.
#
#   건수   2 41 24 4 2 45 92 36 - 246
#   수량   890,000 16,208,000 … - 95,112,000
#   경쟁률 0.59 10.81 3.19 1.02 1.00 5.99 31.46 9.35 - 63.41
#
# 마지막 값이 합계 경쟁률이다(스카이랩스 63.41 = 신청 95,112,000주 ÷ 기준
# 1,500,000주, 본문 주석과 일치). "XXX : 1" 형태를 찾던 패턴으로는 원리적으로
# 잡을 수 없었다 — 그 문자열이 문서에 없다.
#
# 숫자가 3개 이상 나열될 때만 표로 인정한다. 본문에 흔한 주의사항 문구
# ("수요예측 경쟁률에 관한 주의사항")는 뒤에 숫자 나열이 없어 걸리지 않는다.
_RATE_CONTEXT = 320      # 표 제목이 들어오는 거리(실측: 스카이랩스 표 제목까지 ~300자)
_RATE_TABLE_RE = re.compile(r"경쟁률((?:\s+(?:[\d,]+(?:\.\d+)?|-)){3,})")


def extract_competition_from_table(text: str) -> Optional[float]:
    """가격대별 수요예측 참여내역 표에서 **합계 경쟁률**을 뽑는다(순수).

    합계는 나열의 마지막 숫자다. 표 자체가 없으면 None — 없는 값을 만들지 않는다.
    """
    best = None
    for m in _RATE_TABLE_RE.finditer(text):
        # **표 앞 문맥을 본다.** 같은 모양의 표가 유상증자 실권주 일반공모에도
        # 있어서, 문맥을 안 보면 그 청약 경쟁률(클로봇 490.00)을 기관 수요예측
        # 경쟁률로 읽는다 — 실측에서 실제로 그렇게 잡혔다.
        head = text[max(0, m.start() - _RATE_CONTEXT):m.start()]
        if "수요예측" not in head or "일반공모" in head:
            continue
        nums = [x for x in m.group(1).split() if x not in ("-",)]
        if len(nums) < 3:
            continue
        try:
            value = _to_num(nums[-1])
        except ValueError:
            continue
        if value <= 0:
            continue
        # 같은 표가 여러 번 나오면(정정 공시) 가장 큰 합계를 택하지 않는다 —
        # 첫 번째(본문 순서상 최신 정정본이 앞에 온다)를 그대로 쓴다.
        if best is None:
            best = value
    return best


# ─── 유통가능 물량 (2026-08-28 실측) ─────────────────
#
# 저장된 실제 공시에서 확인한 표현들. 청약 **전**에 나오는 증권신고서에 있어서,
# 수요예측 결과를 기다리지 않고 확보할 수 있는 유일한 채점 요소다.
#
#   "상장예정주식수(…) 11,363,649주 중 26.42%에 해당하는 3,002,063주는
#    상장 직후 유통가능 물량에 해당"
#   "2,119,460주(38.42%)는 상장 직후 시장에서 유통가능한 물량에 해당합니다"
#   "8,071,582주는 상장 직후 시장에서 유통가능한 물량이며, 상장예정주식수
#    기준으로 36.93%에 해당합니다"
#   "[기간별 유통가능물량] … 상장일 유통가능 12,652,939 DR 25.6%"
#
# **"상장 직후"에 한정하는 것이 핵심이다.** 같은 문단에 6개월·12개월 후 *누적*
# 비율이 이어진다("6개월 후 2,428,742주(누적 41.56%)"). 그걸 잡으면 유통물량을
# 실제보다 크게 봐서 점수가 낮아진다 — 조용히 틀린 값이 들어간다.

_PCT = r"(\d{1,3}(?:\.\d+)?)"

FLOAT_PATTERNS = (
    r"중\s*" + _PCT + r"\s*%에\s*해당하는\s*[\d,]+\s*주(?:는|가)?\s*상장\s*직후",
    r"[\d,]+\s*주\s*\(\s*" + _PCT + r"\s*%\s*\)\s*(?:는|가)?\s*상장\s*직후",
    r"상장\s*직후[^.]{0,60}?유통가능한?\s*물량이며[^.]{0,40}?상장예정주식수\s*"
    r"기준으로\s*" + _PCT + r"\s*%",
    r"상장일\s*유통가능\s*[\d,]+\s*\S{0,4}?\s*" + _PCT + r"\s*%",
)


def extract_float_ratio(text: str) -> Optional[float]:
    """상장 직후 유통가능 비율(%)을 뽑는다(순수). 없으면 None.

    범위를 벗어난 값(0 이하·100 초과)은 버린다 — 유통비율은 상장예정주식수에
    대한 비율이라 100%를 넘을 수 없다. 넘었다면 다른 숫자를 잡은 것이다.
    """
    for pattern in FLOAT_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if 0 < value <= 100:
            return value
    return None


def extract_ipo_metrics(text: str) -> dict:
    """텍스트에서 IPO 수치 추출. 실패한 필드는 None."""
    out = {
        "competition_rate": None,
        "lockup_ratio": None,
        "offer_band_low": None,
        "offer_band_high": None,
        "final_price": None,
        "float_ratio": None,      # 상장 직후 유통가능 비율(%)
        "above_band": None,  # bool
    }

    # 표가 먼저다 — 실측에서 값이 실제로 있던 형태다.
    out["competition_rate"] = extract_competition_from_table(text)
    if out["competition_rate"] is None:
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
            low, high = _to_num(m.group(1)), _to_num(m.group(2))
            # 하단 > 상단이면 밴드가 아니라 다른 숫자쌍을 잡은 것이다.
            # 뒤집어 담으면 밴드 위치 점수가 통째로 거꾸로 나온다.
            if low > high:
                continue
            out["offer_band_low"], out["offer_band_high"] = low, high
            break

    for pat in PATTERNS["final_price"]:
        m = re.search(pat, text)
        if m:
            out["final_price"] = _to_num(m.group(1))
            break

    out["float_ratio"] = extract_float_ratio(text)

    # ── 확정가 교차검증 (2026-08-28 실측) ──────────────
    #
    # 실제 스캔에서 브릴스 16,500원·네오사피엔스 13,800원이 확정가로 잡혔는데,
    # **둘 다 그 종목의 밴드 하단과 정확히 같았다**(16,500~19,500 / 13,800~15,800).
    # 수요예측 전 증권신고서는 "인수대가는 … 하단인 16,500원 기준으로 산정" 같은
    # 문장에서 하단 금액을 여러 번 언급한다. "확정공모가액" 뒤 30자 안에 그 숫자가
    # 들어오면 확정가로 읽힌다.
    #
    # 확정가가 잘못 채워지면 단계가 "확정"으로 넘어가 밴드 하단 확정(2점)이 매겨지고,
    # **확정되지도 않은 종목이 최저 점수를 받는다.**
    #
    # 그래서 밴드 하단과 정확히 같은 확정가는 **수요예측 결과가 함께 확인될 때만**
    # 믿는다. 스카이랩스는 진짜 하단 미만 확정(10,000)이었고 경쟁률 63.41이 같은
    # 문서에 있었다 — 그 경우는 그대로 통과한다.
    #
    # 밴드 폭이 0인 경우(스팩: 2,000원 단일)는 제외한다. 하단·상단·확정가가 원래
    # 같으므로 이 검사가 의미를 갖지 않는다.
    if (out["final_price"] is not None
            and out["offer_band_low"] is not None
            and out["offer_band_high"] is not None
            and out["offer_band_high"] > out["offer_band_low"]
            and out["final_price"] == out["offer_band_low"]
            and out["competition_rate"] is None):
        out["final_price"] = None

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
        # 50,000자로 자르던 것을 늘렸다. 수요예측 결과 표는 문서 뒷부분에 있는데
        # 앞부분만 저장하니 "값이 없다"와 "샘플이 잘렸다"를 구분할 수 없었다.
        sample_path.write_text(text[:SAMPLE_CHARS], encoding="utf-8")

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

    # 종류 우선순위로 정렬한다 — 최근 20건을 무작정 훑으면 수요예측 결과가
    # 든 [발행조건확정] 문서가 거의 안 걸린다(2026-08-28 실측: 20건 중 1건).
    try:
        from ipo_bot import filing_rank
        uniq.sort(key=lambda f: (filing_rank(f.get("report_nm", "")),
                                 -int(f.get("rcept_dt", "0") or 0)))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 우선순위 정렬 생략: {exc}", file=sys.stderr)

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
    csv_path = PATHS.ipo_demand_validation
    csv_path.parent.mkdir(parents=True, exist_ok=True)
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
