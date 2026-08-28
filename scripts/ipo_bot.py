#!/usr/bin/env python3
"""
IPO봇 (ipo_bot) — 공모주 매력지수 스캐너 (v3.34)

매력지수 5요소 (각 0~20점, 합산 100점 만점):
  1. 수요예측 경쟁률  — DART 공시 파싱
  2. 공모가 밴드 위치 — 확정가가 밴드 어디에 위치하는지
  3. 유통 비율        — 상장 직후 유통 가능 물량 비율 (낮을수록 ↑)
  4. 주관사 티어      — 대형 증권사 주관 여부
  5. 공모 규모(시총)  — 작을수록 수급 부담 적음 (↑)

데이터 소스:
  - KIND (krx.co.kr)      : 청약 일정, 공모가 밴드, 확정가, 시총
  - 38커뮤니케이션         : 청약 일정 백업
  - DART                   : 수요예측 경쟁률, 유통 비율 (dart_demand_parser 재활용)

등급 기준:
  A++ ≥85  /  A+ ≥75  /  A ≥65  /  B ≥50  /  C <50
  미확정 요소 있을 때: 확정 요소만으로 환산 (*)

API:
  fetch_ipo_schedule(days_ahead=30)       → list[IpoItem]
  fetch_dart_metrics(corp_name, rcept_no) → dict
  compute_attraction_score(ipo_data)      → AttractionResult
  scan_upcoming(days_ahead=30, top_n=10)  → list[dict]
  format_result(results, top_n)           → str
  run(action, **kwargs)                   → (str, list)   # 라우터 entrypoint

TODO (다음 세션):
  - DART 밴드 정규식 정밀화 (offer_band_high 패턴 샘플 검증)
  - paper_ui IPO 탭 연동 (/api/ipo)
  - score_demand 구간 세분화
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

from storage_paths import PATHS

log = logging.getLogger("ipo_bot")

# ─── 경로 ────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
CACHE_DIR = PATHS.shareable_cache_dir
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# dart_demand_parser 재활용
sys.path.insert(0, str(SCRIPTS_DIR))


# ─── 주관사 티어 사전 ────────────────────────────────
# Tier1: 국내 5대 IB (주관 실적·브랜드·배정력 최상위)
# Tier2: 중형 증권사 (괜찮은 배분 + 기관 네트워크)
# Tier3: 소형·지방 증권사

_UNDERWRITER_TIER: dict[str, int] = {
    # Tier 1 (점수 20)
    "미래에셋": 1, "미래에셋증권": 1,
    "KB": 1, "KB증권": 1, "KB투자": 1,
    "NH": 1, "NH투자": 1, "NH투자증권": 1,
    "한국투자": 1, "한국투자증권": 1,
    "삼성": 1, "삼성증권": 1,
    # Tier 2 (점수 14)
    "키움": 2, "키움증권": 2,
    "하나": 2, "하나증권": 2, "하나금융투자": 2,
    "대신": 2, "대신증권": 2,
    "신한": 2, "신한금융투자": 2, "신한투자": 2,
    "유안타": 2, "유안타증권": 2,
    "메리츠": 2, "메리츠증권": 2,
    "교보": 2, "교보증권": 2,
    "SK": 2, "SK증권": 2,
    # Tier 3: 위 목록 외 모두 (점수 8)
}
_TIER_SCORE = {1: 20, 2: 14, 3: 8}


def _underwriter_tier(name: str) -> int:
    """주관사 이름 → 티어(1/2/3). 복수 주관사는 가장 높은 티어 반환."""
    if not name:
        return 3
    best = 3
    for key, tier in _UNDERWRITER_TIER.items():
        if key in name:
            best = min(best, tier)
    return best


# ─── 데이터 클래스 ───────────────────────────────────

@dataclass
class IpoItem:
    """단일 공모주 기본 정보"""
    corp_name: str                          # 기업명
    ticker: Optional[str] = None            # 상장 후 종목코드 (미확정이면 None)
    rcept_no: Optional[str] = None          # DART 공시번호
    # 청약 일정
    sub_start: Optional[str] = None        # 청약 시작일 YYYYMMDD
    sub_end: Optional[str] = None          # 청약 종료일 YYYYMMDD
    listing_date: Optional[str] = None     # 상장일 YYYYMMDD
    # 공모가
    band_low: Optional[float] = None       # 공모 희망가 하단
    band_high: Optional[float] = None      # 공모 희망가 상단
    final_price: Optional[float] = None   # 확정 공모가
    # 규모
    total_shares: Optional[int] = None    # 총 공모주식 수
    offer_amount: Optional[float] = None  # 공모금액 (억원) — KIND 공모기업현황 col6
    # 수요예측
    competition_rate: Optional[float] = None  # 기관 수요예측 경쟁률
    lockup_ratio: Optional[float] = None      # 의무보유 확약 비율 (%)
    # 유통
    float_ratio: Optional[float] = None       # 상장 직후 유통 가능 비율 (%)
    # 주관사
    underwriter: Optional[str] = None         # 주관사명
    # 수요예측 일정 (KIND 캘린더)
    demand_start: Optional[str] = None        # 수요예측 시작 YYYYMMDD
    demand_end: Optional[str] = None          # 수요예측 종료 YYYYMMDD
    # 소스 메타
    source: str = "unknown"                   # "kind" / "38" / "dart" / "manual"
    fetched_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@dataclass
class AttractionResult:
    """매력지수 계산 결과"""
    corp_name: str
    # 각 요소 점수 (0~20, None=미확정)
    demand_score: Optional[float] = None       # 수요예측 경쟁률
    band_score: Optional[float] = None        # 공모가 밴드 위치
    float_score: Optional[float] = None       # 유통 비율
    underwriter_score: Optional[float] = None # 주관사 티어
    offer_size_score: Optional[float] = None  # 공모 규모(공모금액)
    # 총점
    total_score: Optional[float] = None       # 확정 요소 합산 (100점 환산)
    grade: str = "?"                           # A++/A+/A/B/C/?
    stage: str = "확정"                        # "사전"(수요예측 전) / "확정"
    confirmed_factors: int = 0                # 확정된 요소 수 (5개 만점)
    note: str = ""                            # 경고·참고 메시지


# ─── 5요소 점수 계산 ─────────────────────────────────

def score_demand(rate: Optional[float]) -> Optional[float]:
    """수요예측 경쟁률 → 0~20점.

    기관 경쟁률이 높을수록 기관 수요가 강하다는 신호.
    AQR-스타일 로그 스케일 근사 (실전 샘플 보정 예정).
    """
    if rate is None:
        return None
    if rate >= 1500:
        return 20.0
    if rate >= 1000:
        return 17.0
    if rate >= 500:
        return 14.0
    if rate >= 200:
        return 11.0
    if rate >= 100:
        return 8.0
    if rate >= 50:
        return 5.0
    return 2.0


def score_band_position(
    band_low: Optional[float],
    band_high: Optional[float],
    final_price: Optional[float],
) -> Optional[float]:
    """공모가 밴드 위치 → 0~20점.

    확정가가 밴드 상단 초과(above band)일수록 기관 수요 초과 신호.
    밴드 내 위치는 (final - low) / (high - low) 비율로 판단.
    """
    if final_price is None:
        return None
    if band_high is not None and final_price > band_high:
        return 20.0  # 상단 초과
    if band_low is None or band_high is None:
        return None
    span = band_high - band_low
    if span <= 0:
        return 10.0  # 단일 밴드
    ratio = (final_price - band_low) / span  # 0.0~1.0
    if ratio >= 0.9:
        return 16.0  # 상단 근접
    if ratio >= 0.6:
        return 12.0  # 상단 지향
    if ratio >= 0.4:
        return 8.0   # 중간
    if ratio >= 0.1:
        return 4.0   # 하단 지향
    return 2.0       # 하단 확정


def score_float_ratio(
    ratio: Optional[float],
    lockup_ratio: Optional[float] = None,
) -> Optional[float]:
    """유통 비율 → 0~20점.

    상장 직후 유통 가능 물량이 적을수록 수급 안정.
    lockup_ratio: 기관 의무보유 확약 비율(%). 있으면 실질 유통비율로 보정.

    보정 공식:
        effective = ratio × (1 - lockup_ratio/100 × 0.65)
        0.65 = 기관 배정분이 float에서 차지하는 비중 가정치
    ratio: 0~100 (%).
    """
    if ratio is None:
        return None
    # 의무보유확약 반영: 기관의 lock-up 만큼 실질 유통 감소
    if lockup_ratio is not None:
        _INST_WEIGHT = 0.65   # 기관 배정분 ≈ 유통가능물량의 65%
        effective = ratio * (1.0 - lockup_ratio / 100.0 * _INST_WEIGHT)
    else:
        effective = ratio
    if effective <= 15:
        return 20.0
    if effective <= 20:
        return 17.0
    if effective <= 25:
        return 14.0
    if effective <= 30:
        return 11.0
    if effective <= 35:
        return 8.0
    if effective <= 40:
        return 5.0
    return 2.0


def score_underwriter(name: Optional[str]) -> Optional[float]:
    """주관사 티어 → 0~20점."""
    if not name:
        return None
    tier = _underwriter_tier(name)
    return float(_TIER_SCORE[tier])


def score_offer_size(offer_amount_eok: Optional[float]) -> Optional[float]:
    """공모 규모(**공모금액**, 억원) → 0~20점.

    2026-08-28에 입력을 시총에서 공모금액으로 바꿨다. 이유는 두 가지다.

      1) **자동으로 얻을 수 있는 값이 공모금액뿐이다.** KIND 공모기업현황이 주는
         것은 공모금액(백만원)이고, 시총을 얻으려면 상장예정주식수가 필요한데
         어느 소스에서도 수집하지 않는다. 공모비율을 가정해 역산하면 근거 없는
         가정 하나가 점수에 섞인다.
      2) **단기 수급 부담을 결정하는 것은 시장에 새로 풀리는 금액**이다. 시총이
         커도 공모금액이 작으면 상장일 매물 부담은 작다. 원래 이 요소의 취지
         ("규모가 작을수록 단기 상승 여력 ↑")에 공모금액이 더 가깝다.

    **임계값은 전부 가설이다.** 시총 기준(500·1000·3000·5000억…)을 그대로 쓸 수 없어
    공모금액 분포를 보고 새로 잡았다(스팩·소형 100억 미만 ~ 대형 1000억 이상).
    표본이 쌓이면 재조정한다.
    """
    if offer_amount_eok is None:
        return None
    if offer_amount_eok <= 100:      # 스팩·초소형
        return 20.0
    if offer_amount_eok <= 200:
        return 17.0
    if offer_amount_eok <= 400:
        return 14.0
    if offer_amount_eok <= 800:
        return 11.0
    if offer_amount_eok <= 1500:
        return 8.0
    if offer_amount_eok <= 3000:
        return 5.0
    return 2.0                       # 대형 공모 — 상장일 매물 부담 큼


def _grade(score: float) -> str:
    if score >= 85:
        return "A++"
    if score >= 75:
        return "A+"
    if score >= 65:
        return "A"
    if score >= 50:
        return "B"
    return "C"


STAGE_PRE = "사전"
STAGE_FINAL = "확정"

# 수요예측 **전에도** 확보 가능한 요소. 나머지 셋(경쟁률·밴드위치·확정가)은
# 수요예측이 끝나야 비로소 존재한다.
MIN_CONFIRMED_PRE = 2      # 주관사 + 공모규모
MIN_CONFIRMED_FINAL = 3


def demand_stage(item: IpoItem) -> str:
    """수요예측 결과가 **실제로 확보됐는지**로 단계를 가른다(순수).

    일정(`demand_end`)이 아니라 **값의 존재**로 판단한다. 일정이 지났다고 결과가
    손에 들어온 것은 아니고, 일정만 보고 "확정 단계"라고 선언하면 확보하지도 못한
    값을 요구하다 등급이 통째로 사라진다 — 2026-08 몇 달간 IPO봇이 아무것도
    다루지 못한 원인이 정확히 이 종류의 어긋남이었다.
    """
    if item.competition_rate is not None or item.final_price is not None:
        return STAGE_FINAL
    return STAGE_PRE


def compute_attraction_score(item: IpoItem) -> AttractionResult:
    """IpoItem → AttractionResult (5요소 계산).

    미확정 요소는 제외하고 확정 요소만으로 100점 환산.

    **필요 확정 요소 수는 단계마다 다르다(2026-08-28).** 5요소 중 셋(경쟁률·
    밴드위치·확정가)은 수요예측이 끝나야 존재하므로, 청약 전 스캔에서는 아무리
    수집이 잘 돼도 최대 2개(주관사·공모규모)뿐이다. 여기에 일률적으로 "3개 이상"을
    요구하면 **청약 전에는 구조적으로 항상 등급 불가**가 된다. 실제로 그랬다.

      - 사전 단계: 2개 이상 → 등급 산출. 정보가 적으므로 `stage="사전"`을 함께 싣고,
        알림·자동 구독은 확정 단계에만 적용한다(과신 방지).
      - 확정 단계: 3개 이상 → 기존과 동일.
    """
    d = score_demand(item.competition_rate)
    b = score_band_position(item.band_low, item.band_high, item.final_price)
    f = score_float_ratio(item.float_ratio, item.lockup_ratio)
    u = score_underwriter(item.underwriter)
    s = score_offer_size(item.offer_amount)

    scores = [d, b, f, u, s]
    confirmed = [v for v in scores if v is not None]
    n = len(confirmed)

    stage = demand_stage(item)
    required = MIN_CONFIRMED_FINAL if stage == STAGE_FINAL else MIN_CONFIRMED_PRE

    total = None
    grade = "?"
    note = ""

    if n >= required:
        # 확정 요소만으로 100점 환산
        max_possible = n * 20
        raw_sum = sum(confirmed)
        total = round(raw_sum / max_possible * 100, 1)
        grade = _grade(total)
        if stage == STAGE_PRE:
            note = (f"📋 사전등급 — 수요예측 전이라 경쟁률·확정가가 없습니다 "
                    f"(확정 {n}/5). 참고용이며 자동 구독 대상이 아닙니다")
        elif n < 5:
            note = f"⚠️ {5-n}개 요소 미확정 — 환산 점수 (확정 {n}/5)"
    else:
        note = (f"⚠️ 확정 요소 {n}개 (필요 {required}개, {stage} 단계) — "
                f"신뢰도 부족, 등급 산출 불가")
        if stage == STAGE_PRE and u is None:
            note += " · 주관사 미확보"
        if stage == STAGE_PRE and s is None:
            note += " · 공모금액 미확보"
    if stage == STAGE_FINAL and item.competition_rate is None:
        note += " · 경쟁률 미확보(DART 파싱 확인 필요)"

    return AttractionResult(
        corp_name=item.corp_name,
        demand_score=d,
        band_score=b,
        float_score=f,
        underwriter_score=u,
        offer_size_score=s,
        total_score=total,
        grade=grade,
        stage=stage,
        confirmed_factors=n,
        note=note,
    )


# ─── KIND 크롤러 (공모일정 캘린더 API) ──────────────────
# API : POST https://kind.krx.co.kr/listinvstg/pubofrschdl.do
# Form: method=searchPubofrScholCalnd&forward=pubofrSchol_sub
#       &marketType=&scholType=&selYear=YYYY&selMonth=MM
# 응답: HTML 캘린더 — <td>일자 → <strong>청약</strong> → 회사명

_KIND_SCHDL_URL = "https://kind.krx.co.kr/listinvstg/pubofrschdl.do"
_KIND_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": (
        "https://kind.krx.co.kr/listinvstg/pubofrschdl.do"
        "?method=searchPubofrScholMain"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


def _kind_fetch_calendar(sel_year: int, sel_month: int,
                         timeout: int = 15) -> Optional[str]:
    """KIND 공모일정 캘린더 HTML 수집 (GET 세션 초기화 → POST)."""
    try:
        import ssl as _ssl
        import requests as _req
        import urllib3
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        class _LegacyAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = create_urllib3_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                ctx.set_ciphers("ALL:@SECLEVEL=0")
                ctx.options |= getattr(_ssl, "OP_LEGACY_SERVER_CONNECT", 0)
                kwargs["ssl_context"] = ctx
                super().init_poolmanager(*args, **kwargs)

        session = _req.Session()
        session.mount("https://", _LegacyAdapter())
        session.mount("http://", _LegacyAdapter())

        # 1단계: GET으로 세션 쿠키 획득
        main_url = _KIND_SCHDL_URL + "?method=searchPubofrScholMain"
        get_hdrs = {
            "User-Agent": _KIND_HEADERS["User-Agent"],
            "Accept-Language": _KIND_HEADERS["Accept-Language"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            session.get(main_url, headers=get_hdrs, timeout=timeout, verify=False)
        except Exception:
            pass  # 쿠키 획득 실패해도 POST 시도

        # 2단계: POST로 달력 데이터 요청
        payload = (
            "method=searchPubofrScholCalnd"
            "&forward=pubofrSchol_sub"
            "&marketType="
            "&scholType="
            f"&selYear={sel_year}"
            f"&selMonth={sel_month:02d}"
        )
        hdrs = {
            **_KIND_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": main_url,
        }

        resp = session.post(
            _KIND_SCHDL_URL, data=payload, headers=hdrs,
            timeout=timeout, verify=False,
        )
        if resp.ok:
            log.debug(f"KIND 캘린더 POST 성공 ({sel_year}-{sel_month:02d}, {resp.status_code})")
            return resp.text

        log.warning(f"KIND 캘린더 요청 실패: HTTP {resp.status_code}")
        return None
    except Exception as e:
        log.warning(f"KIND 캘린더 요청 실패: {e}")
        return None


def _kind_fetch(extra_qs=None, timeout: int = 15) -> Optional[str]:
    today = date.today()
    return _kind_fetch_calendar(today.year, today.month, timeout=timeout)


def _parse_kind_calendar(html: str, sel_year: int, sel_month: int) -> dict:
    """KIND 캘린더 HTML → {회사명: {sub_start, sub_end, market}}."""
    import re as _re
    result: dict = {}
    td_blocks = _re.split(r"<td[^>]*>", html, flags=_re.IGNORECASE)

    for block in td_blocks:
        day_m = _re.match(r"\s*(\d{1,2})\b", block)
        if not day_m:
            continue
        day = int(day_m.group(1))
        if not (1 <= day <= 31):
            continue
        date_str = f"{sel_year}{sel_month:02d}{day:02d}"

        li_items = _re.findall(r"<li[^>]*>(.*?)</li>", block, _re.DOTALL | _re.IGNORECASE)
        i = 0
        while i < len(li_items):
            type_m = _re.search(r"<strong>([^<]+)</strong>", li_items[i], _re.IGNORECASE)
            if type_m and i + 1 < len(li_items):
                event_type = _clean(type_m.group(1))
                next_li = li_items[i + 1]
                a_m = _re.search(r"<a[^>]*>(.*?)</a>", next_li, _re.DOTALL | _re.IGNORECASE)
                if a_m:
                    corp_name = _clean(a_m.group(1))
                    mkt_m = _re.search(r'alt=["\'"]([^"\'"]+)["\'"]', next_li, _re.IGNORECASE)
                    market = mkt_m.group(1) if mkt_m else ""
                    if corp_name and event_type in ("청약", "수요예측", "상장"):
                        if corp_name not in result:
                            result[corp_name] = {"sub_start": None, "sub_end": None,
                                                 "demand_start": None, "demand_end": None,
                                                 "listing_date": None,
                                                 "market": market}
                        d = result[corp_name]
                        if event_type == "상장":
                            # 상장일은 단일 날짜 — 가장 빠른 값 보존
                            if d["listing_date"] is None or date_str < d["listing_date"]:
                                d["listing_date"] = date_str
                        else:
                            sk = "sub_start" if event_type == "청약" else "demand_start"
                            ek = "sub_end"   if event_type == "청약" else "demand_end"
                            if d[sk] is None or date_str < d[sk]:
                                d[sk] = date_str
                            if d[ek] is None or date_str > d[ek]:
                                d[ek] = date_str
                i += 2
                continue
            i += 1
    return result


def fetch_ipo_schedule_kind(days_ahead: int = 30) -> list[IpoItem]:
    """KIND 공모일정 캘린더에서 향후 N일 청약 일정 수집."""
    today    = date.today()
    end_date = today + timedelta(days=days_ahead)

    months_to_fetch: set = set()
    cur = today.replace(day=1)
    while cur <= end_date:
        months_to_fetch.add((cur.year, cur.month))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)

    combined: dict = {}
    for yr, mo in sorted(months_to_fetch):
        html = _kind_fetch_calendar(yr, mo)
        if not html:
            log.warning(f"KIND 캘린더 응답 없음: {yr}-{mo:02d}")
            continue
        month_data = _parse_kind_calendar(html, yr, mo)
        for corp, info in month_data.items():
            if corp not in combined:
                combined[corp] = info
            else:
                d = combined[corp]
                for sk, ek in [("sub_start","sub_end"),("demand_start","demand_end")]:
                    if info[sk] and (d[sk] is None or info[sk] < d[sk]):
                        d[sk] = info[sk]
                    if info[ek] and (d[ek] is None or info[ek] > d[ek]):
                        d[ek] = info[ek]
                # listing_date 병합 — 가장 이른 날짜 채택
                ld_new = info.get("listing_date")
                ld_cur = d.get("listing_date")
                if ld_new and (ld_cur is None or ld_new < ld_cur):
                    d["listing_date"] = ld_new

    today_str = today.strftime("%Y%m%d")
    end_str   = end_date.strftime("%Y%m%d")

    items: list[IpoItem] = []
    for corp_name, info in combined.items():
        ss = info["sub_start"]
        se = info["sub_end"]
        ds = info.get("demand_start")
        de = info.get("demand_end")
        # 청약 날짜 없으면 (수요예측만 있으면) 필터
        if not ss or not (today_str <= ss <= end_str):
            continue
        ld = info.get("listing_date")
        item = IpoItem(corp_name=corp_name, sub_start=ss, sub_end=se,
                       demand_start=ds, demand_end=de,
                       listing_date=ld, source="kind")
        items.append(item)
        log.debug(f"KIND: {corp_name} 청약 {ss}~{se}"
                  + (f" 수요예측 {ds}~{de}" if ds else ""))

    log.info(f"KIND: {len(items)}건 ({days_ahead}일 이내)")
    return items


# ─── 38커뮤니케이션 크롤러 (뼈대) ───────────────────
# TODO (다음 세션): 파서 완성
# URL: https://www.38.co.kr/html/fund/index.htm?o=k

_38_URL = "https://www.38.co.kr/html/fund/index.htm?o=k"
_38_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.38.co.kr/",
}


def _fetch_38_html(timeout: int = 15) -> Optional[str]:
    """38.co.kr HTML 수집.

    38.co.kr는 RSA 키교환 cipher만 지원하는 구형 TLS 서버.
    Python 3.10+ 기본 SECLEVEL=2에서 RSA 차단됨 → SECLEVEL=0 + 커스텀 어댑터 필요.
    """
    try:
        import ssl as _ssl
        import requests as _req
        import urllib3
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        class _LegacyAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = create_urllib3_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                ctx.set_ciphers("ALL:@SECLEVEL=0")
                ctx.options |= getattr(_ssl, "OP_LEGACY_SERVER_CONNECT", 0)
                kwargs["ssl_context"] = ctx
                super().init_poolmanager(*args, **kwargs)

        session = _req.Session()
        session.mount("https://", _LegacyAdapter())
        resp = session.get(_38_URL, headers=_38_HEADERS, timeout=timeout, verify=False)
        resp.encoding = "euc-kr"
        return resp.text
    except Exception as e:
        log.warning(f"38커뮤니케이션 요청 실패: {e}")
        return None


def _clean(s: str) -> str:
    """HTML 태그·공백·엔티티 제거."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"&[a-zA-Z]+;|&#\d+;", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_price(s: str) -> Optional[float]:
    """'12,000' / '12,000~14,000' 에서 숫자 추출 (상단 우선)."""
    nums = re.findall(r"[\d,]+", s)
    if not nums:
        return None
    try:
        return float(nums[-1].replace(",", ""))  # 마지막 숫자 = 상단
    except ValueError:
        return None


def _parse_date_38(s: str, year: int) -> Optional[str]:
    """날짜 파싱: 'YYYY.MM.DD', 'MM.DD', 'YYYY.MM.DD~MM.DD' 등 지원."""
    # 우선 YYYY.MM.DD 전체 형식 탐색 (2026.06.18 같은 패턴)
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{2})", s)
    if m:
        y, mon, day = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return f"{y}{mon:02d}{day:02d}"
    # MM.DD 형식 fallback (월 범위 검증으로 연도 오파싱 방지)
    m = re.search(r"\b(\d{1,2})\.(\d{2})\b", s)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            return f"{year}{mon:02d}{day:02d}"
    return None


def _parse_date_38_end(s: str, year: int) -> Optional[str]:
    """'YYYY.MM.DD~MM.DD' 또는 'MM.DD~MM.DD' 에서 종료일 추출."""
    if "~" not in s:
        return _parse_date_38(s, year)
    start_str, end_str = [p.strip() for p in s.split("~", 1)]
    # 시작일에서 연도 추출 (종료일이 MM.DD만 있을 경우 사용)
    m_year = re.search(r"(\d{4})", start_str)
    base_year = int(m_year.group(1)) if m_year else year
    return _parse_date_38(end_str, base_year)


def _parse_offer_amount(raw: str) -> Optional[float]:
    """KIND 공모금액(백만원) → 억원. 미확정("-", "", "미정")은 None.

    **모르는 것을 0으로 두지 않는다.** 0으로 채우면 "공모금액 0억"이 되어
    규모 점수 20점(최고)을 받는다 — 없는 정보가 최고 점수로 둔갑한다.
    """
    if not raw:
        return None
    text = raw.replace(",", "").strip()
    if text in ("-", "미정", "", "&nbsp;"):
        return None
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    million_won = float(m.group())
    if million_won <= 0:
        return None
    return round(million_won / 100.0, 1)      # 백만원 → 억원


def _parse_38_html(html: str) -> list[IpoItem]:
    """38커뮤니케이션 HTML → IpoItem 목록.

    38커뮤니케이션 공모주 페이지(o=k) 테이블 구조:
      col0: 종목명(링크)  col1: 공모가범위  col2: 확정공모가
      col3: 청약일(시작~종료)  col4: 환불일  col5: 상장일  col6: 주관사
    인코딩: EUC-KR (호출 측에서 디코딩 후 전달).

    파싱 실패 시 debug 로그 → ipo_bot --debug-html로 raw HTML 저장 후 확인.
    """
    items: list[IpoItem] = []
    year = date.today().year

    # 테이블 행 추출
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
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
        band_nums = re.findall(r"[\d,]+", band_raw)
        band_low = float(band_nums[0].replace(",", "")) if len(band_nums) >= 1 else None
        band_high = float(band_nums[-1].replace(",", "")) if len(band_nums) >= 2 else band_low

        # 상장일: 38 목록 페이지에는 없음 → None (개별 페이지에서만 확인 가능)
        listing_date = None

        # 주관사 (col5): "미래에셋증권,이모아증권"
        underwriter = texts[5].split(",")[0].strip() if len(texts) > 5 else None

        item = IpoItem(
            corp_name=corp_name,
            band_low=band_low,
            band_high=band_high,
            final_price=final_price,
            sub_start=sub_start,
            sub_end=sub_end,
            listing_date=listing_date,
            underwriter=underwriter,
            source="38",
        )
        items.append(item)
        log.debug(f"38 파싱: {corp_name} 청약{sub_start}~{sub_end} 상장{listing_date}")

    log.info(f"38커뮤니케이션 파싱: {len(items)}건")
    return items


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
        if it.sub_start and today_str <= it.sub_start <= end_str
    ]
    log.info(
        f"38커뮤니케이션: {len(filtered)}건 "
        f"({days_ahead}일 이내 / 전체 {len(items)}건)"
    )
    return filtered


def fetch_ipo_schedule(days_ahead: int = 30) -> list[IpoItem]:
    """38 청약일 우선 + KIND 캘린더 수요예측 보강 + KIND 공모기업현황 구조 보강.

    소스 우선순위:
      1. 38커뮤니케이션 — retail 청약기간, 공모가 범위, 확정가, 주관사
      2. KIND 캘린더   — 수요예측 기간, 상장일 (캘린더 이벤트)
      3. KIND 공모기업현황 — 공모가·확정가·주관사·상장일 (38 미기재 시 보완)
    """
    from dataclasses import replace as _replace

    kind_items   = fetch_ipo_schedule_kind(days_ahead)
    items_38     = fetch_ipo_schedule_38(days_ahead)
    progcom_items = fetch_ipo_schedule_kind_progcom(days_ahead)

    # ── 캘린더 맵 ──────────────────────────────────────
    kind_map: dict[str, IpoItem] = {it.corp_name: it for it in kind_items}

    # ── 공모기업현황 맵 ────────────────────────────────
    progcom_map: dict[str, IpoItem] = {it.corp_name: it for it in progcom_items}

    # ── 38 기반으로 병합 ───────────────────────────────
    merged: dict[str, IpoItem] = {}
    for item in items_38:
        # KIND 캘린더 → 수요예측 기간 보강
        k = kind_map.get(item.corp_name)
        if k and (k.demand_start or k.demand_end):
            item = _replace(item,
                            demand_start=item.demand_start or k.demand_start,
                            demand_end=item.demand_end or k.demand_end)
        # KIND 캘린더 → 상장일 보강
        if k and k.listing_date and not item.listing_date:
            item = _replace(item, listing_date=k.listing_date)
        # KIND 공모기업현황 → 누락 필드 보강
        p = progcom_map.get(item.corp_name)
        if p:
            item = _replace(item,
                band_low=item.band_low or p.band_low,
                band_high=item.band_high or p.band_high,
                final_price=item.final_price or p.final_price,
                # 공모금액은 KIND 공모기업현황에만 있다. 여기서 옮기지 않으면
                # 38에 실린 종목(대부분)은 규모 요소가 영원히 비어 있게 된다.
                offer_amount=item.offer_amount or p.offer_amount,
                underwriter=item.underwriter or p.underwriter,
                listing_date=item.listing_date or p.listing_date,
                demand_start=item.demand_start or p.demand_start,
                demand_end=item.demand_end or p.demand_end,
            )
        merged[item.corp_name] = item

    # ── KIND에만 있는 종목 추가 ────────────────────────
    added_cal = 0
    for item in kind_items:
        if item.corp_name not in merged:
            # 공모기업현황으로 보강
            p = progcom_map.get(item.corp_name)
            if p:
                item = _replace(item,
                    band_low=item.band_low or p.band_low,
                    band_high=item.band_high or p.band_high,
                    final_price=item.final_price or p.final_price,
                    offer_amount=item.offer_amount or p.offer_amount,
                    underwriter=item.underwriter or p.underwriter,
                    listing_date=item.listing_date or p.listing_date,
                )
            merged[item.corp_name] = item
            added_cal += 1

    # ── 공모기업현황에만 있는 종목 추가 ──────────────
    added_prog = 0
    for item in progcom_items:
        if item.corp_name not in merged:
            k = kind_map.get(item.corp_name)
            if k:
                item = _replace(item,
                    demand_start=item.demand_start or k.demand_start,
                    demand_end=item.demand_end or k.demand_end,
                    listing_date=item.listing_date or k.listing_date,
                )
            merged[item.corp_name] = item
            added_prog += 1

    # ── listing_date < sub_start 모순 제거 ───────────────
    # 두 소스 날짜 불일치로 상장일이 청약일보다 앞서는 경우 listing_date 무효화
    from dataclasses import replace as _replace2
    result_list = []
    for item in merged.values():
        if item.listing_date and item.sub_start and item.listing_date < item.sub_start:
            log.warning(
                f"{item.corp_name}: listing_date({item.listing_date}) < sub_start({item.sub_start}) "
                f"→ 소스 불일치, listing_date 무효화"
            )
            item = _replace2(item, listing_date=None)
        result_list.append(item)

    log.info(
        f"병합: 38 {len(items_38)}건 + KIND캘린더 신규 {added_cal}건 "
        f"+ KIND공모기업현황 신규 {added_prog}건 = 총 {len(result_list)}건"
    )
    return result_list


# ─── KIND 공모기업현황 크롤러 ──────────────────────────
# POST https://kind.krx.co.kr/listinvstg/pubofrprogcom.do
# method=searchPubofrProgComList → HTML 테이블
# 캘린더(날짜만)보다 구조화된 데이터: 공모가 범위·확정가·주관사·상장일 포함

_KIND_PROGCOM_URL = "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do"


def _kind_fetch_ipo_progcom(
    from_date: str,
    to_date: str,
    market_type: str = "",
    timeout: int = 20,
    raw_payload: Optional[str] = None,
) -> Optional[str]:
    """KIND 공모기업현황 목록 HTML (GET 세션 초기화 → POST).

    from_date/to_date: YYYYMMDD (신고서제출일 기준 범위).
    raw_payload: DevTools로 캡처한 정확한 POST body를 직접 주입 (디버깅용).
    성공 시 HTML fragment 반환. 실패 시 None.
    """
    import time as _time

    try:
        import ssl as _ssl
        import requests as _req
        import urllib3
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        class _LegacyAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = create_urllib3_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                ctx.set_ciphers("ALL:@SECLEVEL=0")
                ctx.options |= getattr(_ssl, "OP_LEGACY_SERVER_CONNECT", 0)
                kwargs["ssl_context"] = ctx
                super().init_poolmanager(*args, **kwargs)

        session = _req.Session()
        session.mount("https://", _LegacyAdapter())
        session.mount("http://", _LegacyAdapter())

        main_url = _KIND_PROGCOM_URL + "?method=searchPubofrProgComMain"

        # 1단계: GET으로 세션 쿠키(JSESSIONID) 획득 — 브라우저와 동일한 Accept 헤더 사용
        get_hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8,"
                      "application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            resp_get = session.get(main_url, headers=get_hdrs, timeout=timeout, verify=False)
            log.debug(
                f"KIND progcom GET 완료: status={resp_get.status_code} "
                f"cookies={dict(session.cookies)}"
            )
            _time.sleep(1.0)   # 서버가 세션 준비할 시간
        except Exception as e:
            log.debug(f"KIND progcom GET 세션 초기화 실패 (무시): {e}")

        # 2단계: POST 헤더 (브라우저 XHR과 동일하게)
        post_hdrs = {
            "User-Agent": get_hdrs["User-Agent"],
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://kind.krx.co.kr",
            "Referer": main_url,
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        # DevTools 캡처 결과: fromDate/toDate는 YYYY-MM-DD 포맷 (YYYYMMDD→변환)
        def _fmt_date(d: str) -> str:
            """YYYYMMDD → YYYY-MM-DD (이미 변환된 경우 그대로)"""
            d = d.replace("-", "")
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d

        from_dt_fmt = _fmt_date(from_date)
        to_dt_fmt   = _fmt_date(to_date)

        # DevTools 실캡처 기준 정확한 공통 파라미터 (method/forward 제외)
        base_params = (
            "&currentPageSize="
            "&pageIndex=1"
            "&orderMode=1"
            "&orderStat=D"
            "&searchMode="
            "&searchCodeType="
            "&searchCorpName="
            "&isurCd="
            "&repIsuSrtCd="
            "&bzProcsNo="
            "&detailMarket="
            f"&marketType={market_type}"
            "&searchCorpNameTmp="
            "&repMajAgntDesignAdvserComp="
            "&repMajAgntComp="
            "&designAdvserComp="
            f"&fromDate={from_dt_fmt}"
            f"&toDate={to_dt_fmt}"
        )

        # raw_payload 직접 주입 모드 (DevTools 캡처 payload 그대로 사용)
        if raw_payload:
            candidates_to_try = [(None, None, raw_payload)]
            log.info(f"KIND progcom raw_payload 모드: {raw_payload[:80]}...")
        else:
            # DevTools 실캡처 확인: method=searchPubofrProgComSub, forward=pubofrprogcom_sub
            _CANDIDATES = [
                ("searchPubofrProgComSub",     "pubofrprogcom_sub"),    # ✅ DevTools 실캡처 정확한 값
                ("searchPubofrProgComList",    "pubofrprogcom_list"),   # 소문자 변형
                ("searchPubofrProgComList",    "pubofrProgCom_list"),   # 기존 후보
                ("searchPubofrProgComList",    "pubofrProgCom_sub"),
                ("searchPubofrProgComSub",     "pubofrProgCom_sub"),    # 대소문자 혼용
                ("searchPubofrProgComSrch",    "pubofrProgCom_list"),
                ("searchPubofrProgComSub",     "pubofrProgCom_list"),
                ("searchPubofrProgComCalnd",   "pubofrProgCom_list"),
                ("searchPubofrProgComView",    "pubofrProgCom_list"),
                ("searchPubofrProgComInq",     "pubofrProgCom_list"),
            ]
            candidates_to_try = [(m, f, None) for m, f in _CANDIDATES]

        for method_nm, forward_nm, payload_override in candidates_to_try:
            if payload_override is not None:
                payload = payload_override
                label = "raw_payload"
            else:
                payload = f"method={method_nm}&forward={forward_nm}" + base_params
                label = f"{method_nm}/{forward_nm}"

            try:
                resp = session.post(
                    _KIND_PROGCOM_URL, data=payload, headers=post_hdrs,
                    timeout=timeout, verify=False,
                )
            except Exception as e:
                log.debug(f"KIND progcom 요청 오류 ({label}): {e}")
                continue

            if not resp.ok:
                log.debug(f"KIND progcom HTTP {resp.status_code} ({label})")
                continue

            text = resp.text
            if "페이지 오류" in text or "존재하지 않습니다" in text or "잠시 후" in text:
                log.debug(f"KIND progcom 에러 페이지 ({label}) — 다음 후보 시도")
                continue
            if len(text.strip()) < 200:
                log.debug(
                    f"KIND progcom 응답 너무 짧음 {len(text)}자 ({label}) — 다음 후보 시도"
                )
                continue

            log.info(f"KIND 공모기업현황 POST 성공 → {label}, {len(text)}자")
            return text

        log.warning(
            "KIND 공모기업현황: 모든 후보 실패. "
            "debug-html --source progcom --raw-payload '<payload>' 로 정확한 파라미터를 주입하거나, "
            "브라우저 DevTools → Network → XHR로 정확한 POST body를 확인하세요."
        )
        return None
    except Exception as e:
        log.warning(f"KIND 공모기업현황 요청 실패: {e}")
        return None




def _parse_date_kind(s: str, take_end: bool = False) -> Optional[str]:
    """KIND 공모기업현황 날짜 파싱.

    입력 형식: "2026-05-18 ~ 2026-05-22"  또는 "2026-06-04"
    반환: "YYYYMMDD" 문자열 또는 None.
    take_end=True이면 '~' 뒤 종료일 반환.
    """
    if not s or s.strip() in ("-", ""):
        return None
    parts = [p.strip() for p in s.split("~")]
    target = parts[-1] if (take_end and len(parts) > 1) else parts[0]
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", target)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return None


def _parse_kind_progcom_html(html: str) -> list[IpoItem]:
    """KIND 공모기업현황 HTML 테이블 → IpoItem 목록.

    KIND 공모기업현황 테이블 컬럼 구조 (2026.05 DevTools 실캡처 기준):
      col0: 회사명
      col1: 신고서제출일   (YYYY-MM-DD)
      col2: 수요예측일정   (YYYY-MM-DD ~ YYYY-MM-DD)
      col3: 청약일정       (YYYY-MM-DD ~ YYYY-MM-DD)
      col4: 납입일         (YYYY-MM-DD)
      col5: 확정공모가     (숫자 or "-")
      col6: 공모금액(백만원) (숫자 or "-")
      col7: 상장예정일     (YYYY-MM-DD)
      col8: 상장주선인/지정자문인

    파싱 실패 시 → `python ipo_bot.py debug-html --source progcom` 으로
    raw HTML 저장 후 컬럼 순서 재확인.
    """
    items: list[IpoItem] = []

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 8:
            continue
        texts = [_clean(c) for c in cells]

        # col0: 회사명 (title 속성 우선, 없으면 텍스트)
        m_title = re.search(r'title="([^"]+)"', cells[0])
        corp_name = m_title.group(1).strip() if m_title else texts[0]
        if not corp_name or corp_name in ("회사명", "종목명", ""):
            continue

        # col2: 수요예측일정
        demand_raw   = texts[2] if len(texts) > 2 else ""
        demand_start = _parse_date_kind(demand_raw, take_end=False)
        demand_end   = _parse_date_kind(demand_raw, take_end=True)

        # col3: 청약일정
        sub_raw   = texts[3] if len(texts) > 3 else ""
        sub_start = _parse_date_kind(sub_raw, take_end=False)
        sub_end   = _parse_date_kind(sub_raw, take_end=True)

        # col5: 확정공모가
        final_raw   = texts[5] if len(texts) > 5 else ""
        final_price = (
            _parse_price(final_raw)
            if final_raw and final_raw not in ("-", "미정", "")
            else None
        )

        # col6: 공모금액 (백만원) — 2026-08-28 추가.
        # 여기 값이 있는데도 읽지 않고 있었다. 채점 5요소 중 '규모'가 늘 비어
        # 있었던 직접적인 원인이다. 억원으로 환산해 싣는다(백만원 ÷ 100).
        amount_raw = texts[6] if len(texts) > 6 else ""
        offer_amount = _parse_offer_amount(amount_raw)

        # col7: 상장예정일
        listing_raw  = texts[7] if len(texts) > 7 else ""
        listing_date = _parse_date_kind(listing_raw)

        # col8: 주관사
        underwriter = (
            texts[8].split(",")[0].strip() if len(texts) > 8 and texts[8] else None
        )

        item = IpoItem(
            corp_name=corp_name,
            demand_start=demand_start,
            demand_end=demand_end,
            sub_start=sub_start,
            sub_end=sub_end,
            listing_date=listing_date,
            band_low=None,
            band_high=None,
            final_price=final_price,
            offer_amount=offer_amount,
            underwriter=underwriter,
            source="kind_progcom",
        )
        items.append(item)
        log.debug(
            f"KIND 공모기업현황: {corp_name}  "
            f"청약 {sub_start}~{sub_end}  상장 {listing_date}  "
            f"확정가 {final_price}  공모 {offer_amount}억"
        )

    log.info(f"KIND 공모기업현황 파싱: {len(items)}건")
    return items


def fetch_ipo_schedule_kind_progcom(days_ahead: int = 30) -> list[IpoItem]:
    """KIND 공모기업현황에서 청약 일정 수집 (days_ahead 이내).

    캘린더 API보다 구조화된 데이터 (공모가·주관사·상장일 포함) 를 제공.
    sub_start 기준 필터. sub_start 미확정 종목은 포함.
    """
    today     = date.today()
    end_date  = today + timedelta(days=days_ahead)
    # 신고서 제출일 범위: 최근 90일 ~ 향후 N일 (진행 중 종목 포함)
    from_date = (today - timedelta(days=90)).strftime("%Y%m%d")
    to_date   = end_date.strftime("%Y%m%d")

    html = _kind_fetch_ipo_progcom(from_date, to_date)
    if not html:
        log.warning("KIND 공모기업현황 응답 없음")
        return []

    items = _parse_kind_progcom_html(html)

    # sub_start 기준 days_ahead 이내만 반환 (없으면 포함)
    today_str = today.strftime("%Y%m%d")
    end_str   = end_date.strftime("%Y%m%d")
    filtered  = [
        it for it in items
        if it.sub_start is None or (today_str <= it.sub_start <= end_str)
    ]
    log.info(
        f"KIND 공모기업현황: {len(filtered)}건 "
        f"({days_ahead}일 이내 / 전체 {len(items)}건)"
    )
    return filtered


# ─── DART 수요예측 연동 ──────────────────────────────

# DART 메트릭 캐시 파일 (일별 TTL)
_DART_CACHE_FILE = CACHE_DIR / "dart_metrics_cache.json"
_DART_CACHE_TTL_DAYS = 1  # 하루 지나면 재시도


def _load_dart_cache() -> dict:
    """캐시 파일 로드. 없거나 손상 시 빈 dict."""
    try:
        if _DART_CACHE_FILE.exists():
            return json.loads(_DART_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_dart_cache(cache: dict) -> None:
    try:
        _DART_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"DART 캐시 저장 실패: {e}")


def _normalize_corp_name(name: str) -> str:
    """기업명에서 법인 형태 표기 제거 → DART corp_name 매칭 개선.

    예) '(주)에이치엘비' → '에이치엘비'
        '주식회사 OOO' → 'OOO'
        'OOO Co., Ltd.' → 'OOO'
    """
    s = name.strip()
    # 앞의 (주) / 주식회사 제거
    s = re.sub(r"^(?:\(주\)|주식회사)\s*", "", s)
    # 뒤의 법인 표기 제거
    s = re.sub(
        r"\s*(?:\(주\)|주식회사|Co\.\s*,?\s*Ltd\.?|Inc\.?|Corp\.?)$",
        "", s, flags=re.IGNORECASE,
    )
    return s.strip()


def _demand_forecast_done(item: IpoItem) -> bool:
    """수요예측이 끝났는지(=DART에 결과가 올라왔을 시점인지) 확인.

    demand_end 미확정 종목은 DART에 결과가 없을 가능성이 높으므로
    False 반환 → 불필요한 API 호출 방지.
    단, 아래는 수요예측 종료가 확정이므로 True:
      - rcept_no가 이미 있으면 직접 조회
      - 확정공모가(final_price)가 이미 잡혀 있으면 수요예측 종료 확정
        (KIND progcom이 확정가만 주고 demand_end는 누락하는 케이스 보강)
    """
    if item.rcept_no:
        return True
    if item.final_price is not None:
        return True
    if not item.demand_end:
        return False
    today_str = date.today().strftime("%Y%m%d")
    return item.demand_end < today_str


def fetch_dart_metrics(
    corp_name: str,
    rcept_no: Optional[str] = None,
    days_back: int = 90,
) -> dict:
    """DART 공시에서 수요예측 수치 추출 (캐시 적용).

    dart_demand_parser.py의 list_ipo_filings + extract_ipo_metrics 재활용.
    rcept_no 없으면 corp_name으로 공시 검색 후 가장 최근 것 사용.

    반환 dict 키: competition_rate, lockup_ratio,
                  offer_band_low, offer_band_high, final_price, above_band
    """
    try:
        import dart_demand_parser as ddp
    except ImportError as e:
        log.error(f"dart_demand_parser import 실패: {e}")
        return {}

    # rcept_no 직접 지정 — 캐시 우회 (정확한 조회)
    if rcept_no:
        result = ddp.parse_one(rcept_no, save_text=False)
        return {k: v for k, v in result.items()
                if k not in ("rcept_no", "text_len", "error")}

    # 캐시 확인 (corp_name 기준)
    today_str = date.today().isoformat()
    cache = _load_dart_cache()
    cached = cache.get(corp_name)
    if cached:
        cache_date = cached.get("date", "")
        cache_age = (
            date.today() - date.fromisoformat(cache_date)
        ).days if cache_date else 999
        if cache_age < _DART_CACHE_TTL_DAYS:
            hit_metrics = cached.get("metrics", {})
            if hit_metrics:  # 데이터가 있을 때만 캐시 사용 (빈 결과는 재시도)
                log.debug(f"DART 캐시 HIT: '{corp_name}' (age={cache_age}d)")
                return hit_metrics
            # 빈 결과도 당일 이내면 재호출 방지
            if cache_age == 0:
                log.debug(f"DART 캐시 SKIP (오늘 시도 완료, 결과 없음): '{corp_name}'")
                return {}

    # corp_name으로 최근 공시 검색
    end = date.today()
    start = end - timedelta(days=days_back)
    try:
        filings = ddp.list_ipo_filings(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        )
    except Exception as e:
        log.warning(f"DART 공시 목록 조회 실패: {e}")
        return {}

    # 기업명 일치 필터 — 정규화 후 부분 문자열 매칭 (양방향)
    norm_query = _normalize_corp_name(corp_name)
    matches = []
    for f in filings:
        dart_name = f.get("corp_name", "")
        norm_dart = _normalize_corp_name(dart_name)
        # 어느 쪽이 다른 쪽을 포함하면 매칭
        if norm_query in norm_dart or norm_dart in norm_query:
            matches.append(f)

    if not matches:
        log.info(f"DART: '{corp_name}' 관련 공시 없음 (최근 {days_back}일)")
        cache[corp_name] = {"date": today_str, "metrics": {}}
        _save_dart_cache(cache)
        return {}

    # 가장 최근 공시 사용
    latest = sorted(matches, key=lambda x: x.get("rcept_dt", ""), reverse=True)[0]
    log.info(
        f"DART: '{corp_name}' → '{latest.get('corp_name')}' "
        f"— {latest.get('report_nm')} ({latest.get('rcept_dt')})"
    )
    result = ddp.parse_one(latest["rcept_no"], save_text=False)
    metrics = {k: v for k, v in result.items()
               if k not in ("rcept_no", "text_len", "error")}

    # 캐시 저장 (성공·실패 모두)
    cache[corp_name] = {"date": today_str, "metrics": metrics}
    _save_dart_cache(cache)

    return metrics


def enrich_with_dart(item: IpoItem) -> IpoItem:
    """IpoItem에 DART 수요예측 데이터 보강.

    공모가 밴드(band_low/band_high)는 KIND progcom·캘린더에 없으므로,
    38커뮤니케이션에 없는 종목은 DART 증권신고서 본문에서 보완한다.
    (공모 희망가 밴드는 수요예측 이전 증권신고서에 이미 기재됨)
    """
    metrics = fetch_dart_metrics(item.corp_name, rcept_no=item.rcept_no)
    if not metrics:
        return item
    if item.competition_rate is None and metrics.get("competition_rate"):
        item.competition_rate = metrics["competition_rate"]
    if item.lockup_ratio is None and metrics.get("lockup_ratio"):
        item.lockup_ratio = metrics["lockup_ratio"]
    # ── 공모가 밴드 보완 (KIND progcom·캘린더 미제공) ──
    band_before = (item.band_low, item.band_high)
    if item.band_low is None and metrics.get("offer_band_low"):
        item.band_low = metrics["offer_band_low"]
    if item.band_high is None and metrics.get("offer_band_high"):
        item.band_high = metrics["offer_band_high"]
    if (item.band_low, item.band_high) != band_before:
        log.info(
            f"{item.corp_name}: DART 증권신고서에서 공모가 밴드 보완 "
            f"→ {item.band_low}~{item.band_high}"
        )
    if item.final_price is None and metrics.get("final_price"):
        item.final_price = metrics["final_price"]
    return item


# ─── 스캔 진단 (침묵 실패 방지) ──────────────────────


UNGRADED = "?"


def diagnose_scan(results: list[dict], min_grade: set) -> dict:
    """스캔 결과 → **왜 알림이 안 나가는지**(순수).

    2026-08 실측에서 드러난 것: IPO봇은 몇 달째 아무것도 알리지 않았는데,
    처음엔 "수집 0건"이라고 판단했다가 사용자 맥에서 실제로 돌려 보니 **후보 10건이
    모두 잡히고 있었다.** 막힌 곳은 수집이 아니라 **등급 산출**이었다 —
    10건 전부 `[?] 산출불가(확정 1/5, 주관사만)`이고, 필터는 A등급 이상만 통과시키므로
    영원히 0건이 된다.

    이 셋은 서로 다른 상황이고 대응도 다르다. 하나로 뭉쳐 "알림 없음"으로 끝내면
    고장이 정상처럼 보인다.

      - `no_results`     수집 0건 → 데이터 소스 점검 (파서·차단)
      - `all_ungraded`   후보는 있으나 전원 등급 산출불가 → **채점 입력이 없는 것**
                         (밴드·유통·시총·경쟁률 미확보). 판단을 못 한 것이지
                         "매력 없음"이 아니다.
      - `no_hot`         등급은 나왔고 기준 미달 → **정상 동작**
      - `hot`            통과 종목 있음
    """
    if not results:
        return {"verdict": "no_results", "n": 0, "n_ungraded": 0,
                "grades": [], "hot": [], "preview": []}
    grades = sorted({(r.get("grade") or UNGRADED) for r in results})
    ungraded = [r for r in results if (r.get("grade") or UNGRADED) == UNGRADED]

    # **자동 구독은 확정 단계에서만.** 사전등급은 요소 2개로 낸 값이라 확정 등급과
    # 같은 임계값으로 다루면 정보가 거의 없는 종목이 A++로 올라온다.
    hot = [r for r in results
           if r.get("grade", UNGRADED) in min_grade
           and r.get("stage", STAGE_FINAL) == STAGE_FINAL]
    preview = [r for r in results
               if r.get("grade", UNGRADED) in min_grade
               and r.get("stage") == STAGE_PRE]

    if hot:
        verdict = "hot"
    elif len(ungraded) == len(results):
        verdict = "all_ungraded"
    elif preview:
        verdict = "preview_only"      # 사전등급 후보만 있음 — 알리되 구독은 안 함
    else:
        verdict = "no_hot"
    return {"verdict": verdict, "n": len(results), "n_ungraded": len(ungraded),
            "grades": grades, "hot": hot, "preview": preview}


def missing_factors(results: list[dict]) -> list[str]:
    """등급을 못 낸 이유 — 어떤 채점 요소가 비어 있는지(순수).

    "산출불가"만 보고서는 무엇을 고쳐야 할지 알 수 없다. 비어 있는 요소 이름을
    돌려주어, 파서를 고칠 곳(38·KIND·DART 중 어디)이 드러나게 한다.
    """
    labels = {"demand_score": "경쟁률", "band_score": "공모가밴드",
              "float_score": "유통물량", "underwriter_score": "주관사",
              "offer_size_score": "공모금액"}
    out = []
    for key, label in labels.items():
        if results and all(r.get(key) is None for r in results):
            out.append(label)
    return out


# ─── 통합 스캔 ───────────────────────────────────────

def scan_upcoming(
    days_ahead: int = 30,
    top_n: int = 10,
    enrich_dart: bool = True,
) -> list[dict]:
    """향후 N일 공모주 스캔 → 매력지수 Top N 정렬.

    각 결과 dict:
      corp_name, listing_date, sub_start, sub_end,
      final_price, band_low, band_high, offer_amount,
      underwriter, competition_rate, float_ratio,
      grade, total_score, confirmed_factors, note,
      demand_score, band_score, float_score,
      underwriter_score, offer_size_score
    """
    items = fetch_ipo_schedule(days_ahead)
    if not items:
        log.warning("수집된 IPO 일정 없음 (KIND/38 파서 구현 대기)")
        return []

    results = []
    for item in items:
        # DART 보강 트리거:
        #   (1) 수요예측 종료 → 경쟁률·확약·확정가 등 전체 보강
        #   (2) 공모가 밴드 미확정 → KIND progcom·캘린더엔 밴드가 없으므로
        #       38커뮤니케이션 미수록 종목은 DART 증권신고서에서 밴드만이라도
        #       보완 (밴드는 수요예측 이전 증권신고서에 이미 존재)
        needs_dart = enrich_dart and (
            _demand_forecast_done(item)
            or item.band_low is None
            or item.band_high is None
        )
        if needs_dart:
            try:
                item = enrich_with_dart(item)
            except Exception as e:
                log.warning(f"{item.corp_name} DART 보강 실패: {e}")

        score_result = compute_attraction_score(item)
        row = asdict(item)
        row.update(asdict(score_result))
        results.append(row)

    # 총점 기준 정렬 (None은 뒤로)
    results.sort(
        key=lambda r: r.get("total_score") or -1,
        reverse=True,
    )
    return results[:top_n]


# ─── 출력 포맷 ───────────────────────────────────────

_GRADE_EMOJI = {
    "A++": "🏆", "A+": "🥇", "A": "🥈", "B": "🥉", "C": "📉", "?": "❓",
}


def format_result(results: list[dict], top_n: Optional[int] = None) -> str:
    """스캔 결과 → 텔레그램·웹 UI용 텍스트."""
    if not results:
        return (
            "📋 IPO봇 공모주 스캔\n"
            "(결과 없음 — KIND/38 파서 구현 대기 또는 청약 일정 없음)\n\n"
            "수동 입력 예시:\n"
            "  /ipo 분석 기업명=OOO 경쟁률=1200 공모가=15000 "
            "밴드하단=13000 밴드상단=15000 시총=800 주관사=미래에셋"
        )

    n = len(results)
    lines = [
        f"📋 IPO봇 공모주 매력지수 — Top {n}",
        f"기준일: {date.today().strftime('%Y-%m-%d')}",
        "",
    ]
    for i, r in enumerate(results, 1):
        grade = r.get("grade", "?")
        emoji = _GRADE_EMOJI.get(grade, "❓")
        name = r.get("corp_name", "")
        score = r.get("total_score")
        score_str = f"{score:.0f}점" if score is not None else "산출불가"
        sub_s = r.get("sub_start") or "?"
        sub_e = r.get("sub_end") or "?"
        listing = r.get("listing_date") or "?"
        fp = r.get("final_price")
        bl = r.get("band_low")
        bh = r.get("band_high")
        if fp:
            price_str = f"{fp:,.0f}원(확정)"
        elif bl and bh:
            price_str = (
                f"{bl:,.0f}원(밴드)" if bl == bh
                else f"{bl:,.0f}~{bh:,.0f}원(밴드)"
            )
        else:
            price_str = "미확정"
        amt = r.get("offer_amount")
        amt_str = f"공모 {amt:,.0f}억" if amt else "?"
        uw = r.get("underwriter") or "?"
        n_conf = r.get("confirmed_factors", 0)
        note = r.get("note", "")

        # 요소별 점수 한 줄 요약
        def _s(key):
            v = r.get(key)
            return f"{v:.0f}" if v is not None else "-"

        factor_line = (
            f"경쟁률:{_s('demand_score')} "
            f"밴드:{_s('band_score')} "
            f"유통:{_s('float_score')} "
            f"주관:{_s('underwriter_score')} "
            f"규모:{_s('offer_size_score')}"
        )

        lines += [
            f"{i}. {emoji} {name}  [{grade}] {score_str} ({n_conf}/5)",
            f"   청약 {sub_s}~{sub_e}  상장 {listing}",
            f"   공모가 {price_str}  시총 {amt_str}  주관 {uw}",
            f"   {factor_line}",
        ]
        if note:
            lines.append(f"   {note}")
        lines.append("")

    return "\n".join(lines)


# ─── 단일 종목 수동 분석 ──────────────────────────────

def analyze_manual(
    corp_name: str,
    competition_rate: Optional[float] = None,
    band_low: Optional[float] = None,
    band_high: Optional[float] = None,
    final_price: Optional[float] = None,
    float_ratio: Optional[float] = None,
    lockup_ratio: Optional[float] = None,  # 기관 의무보유 확약 비율 (%)
    underwriter: Optional[str] = None,
    offer_amount: Optional[float] = None,  # 공모금액 (억원)
    **kwargs,
) -> tuple[str, list]:
    """수동 입력 데이터로 단일 종목 매력지수 산출."""
    item = IpoItem(
        corp_name=corp_name,
        competition_rate=competition_rate,
        band_low=band_low,
        band_high=band_high,
        final_price=final_price,
        float_ratio=float_ratio,
        lockup_ratio=lockup_ratio,
        underwriter=underwriter,
        offer_amount=offer_amount,
        source="manual",
    )
    result = compute_attraction_score(item)
    grade = result.grade
    emoji = _GRADE_EMOJI.get(grade, "❓")
    score_str = f"{result.total_score:.1f}점" if result.total_score is not None else "산출불가"

    def _fmt(v):
        return f"{v:.0f}" if v is not None else "미확정"

    lines = [
        f"📋 {corp_name} 매력지수 분석",
        f"{emoji} [{grade}] {score_str} (확정요소 {result.confirmed_factors}/5)",
        "",
        f"① 수요예측 경쟁률: {_fmt(result.demand_score)}/20"
        + (f"  (경쟁률 {competition_rate:.0f}:1)" if competition_rate else "  (미확정)"),
        f"② 공모가 밴드 위치: {_fmt(result.band_score)}/20"
        + (f"  (밴드 {band_low:,.0f}~{band_high:,.0f}원)"
           if band_low and band_high else "  (미확정)"),
        f"③ 유통 비율:        {_fmt(result.float_score)}/20"
        + (f"  ({float_ratio:.1f}%"
           + (f", 확약{lockup_ratio:.0f}%→실질{float_ratio*(1-lockup_ratio/100*0.65):.1f}%)" if lockup_ratio else ")")
           if float_ratio else "  (미확정)"),
        f"④ 주관사 티어:      {_fmt(result.underwriter_score)}/20"
        + (f"  ({underwriter})" if underwriter else "  (미확정)"),
        f"⑤ 공모 규모(공모금액): {_fmt(result.offer_size_score)}/20"
        + (f"  ({offer_amount:,.0f}억원)" if offer_amount else "  (미확정)"),
    ]
    if result.note:
        lines += ["", result.note]
    return ("\n".join(lines), [])


# ─── 라우터 entrypoint ───────────────────────────────

VALID_ACTIONS = {"scan", "analyze"}


def run(
    action: str = "scan",
    corp_name: Optional[str] = None,
    days_ahead: int = 30,
    top_n: int = 10,
    **kwargs,
) -> tuple[str, list]:
    """라우터에서 호출하는 단일 entrypoint.

    action="scan"    : 향후 N일 공모주 스캔 → 매력지수 Top N
    action="analyze" : 단일 종목 수동 입력 분석 (corp_name 필수)

    반환: (사용자 표시용 텍스트, sources=[])
    """
    action = (action or "scan").strip().lower()
    if action not in VALID_ACTIONS:
        return (
            f"❌ 알 수 없는 action: {action!r} (허용: {sorted(VALID_ACTIONS)})", []
        )

    if action == "analyze":
        if not corp_name:
            return ("❌ analyze 액션은 corp_name 필수입니다.", [])
        return analyze_manual(corp_name=corp_name, **kwargs)

    # action == "scan"
    try:
        top_n_val = max(1, min(20, int(top_n)))
        days_val = max(1, min(90, int(days_ahead)))
    except (TypeError, ValueError):
        top_n_val, days_val = 10, 30

    try:
        results = scan_upcoming(days_ahead=days_val, top_n=top_n_val)
    except Exception as e:
        log.exception("ipo_bot scan 실패")
        return (f"❌ IPO봇 스캔 실패: {e}", [])

    return (format_result(results, top_n=top_n_val), results)


# ─── CLI ────────────────────────────────────────────

def _cmd_debug_html(source: str = "38", raw_payload: Optional[str] = None) -> None:
    """raw HTML 저장 → 파서 구조 확인용 (개발 도구).

    source="38"      → 38커뮤니케이션 목록 HTML
    source="kind"    → KIND 공모일정 캘린더 HTML
    source="progcom" → KIND 공모기업현황 테이블 HTML (컬럼 구조 확인 필수)
    """
    save_dir = PATHS.shareable_samples_dir
    save_dir.mkdir(parents=True, exist_ok=True)
    import re as _re

    if source == "38":
        html = _fetch_38_html()
        if not html:
            print("❌ 38커뮤니케이션 응답 없음")
            return
        path = save_dir / "38_raw.html"
        path.write_text(html, encoding="utf-8")
        print(f"✅ 38커뮤니케이션 HTML 저장: {path}")
        rows = _re.findall(r"<tr", html, _re.IGNORECASE)
        print(f"   <tr> 개수: {len(rows)}")

    elif source == "progcom":
        today    = date.today()
        end_date = today + timedelta(days=90)
        from_dt  = (today - timedelta(days=90)).strftime("%Y%m%d")
        to_dt    = end_date.strftime("%Y%m%d")
        if raw_payload:
            print(f"🔧 raw_payload 모드: {raw_payload[:80]}...")
        html = _kind_fetch_ipo_progcom(from_dt, to_dt, raw_payload=raw_payload)
        if not html:
            print("❌ KIND 공모기업현황 응답 없음")
            print("   → 아래 JS를 Chrome 콘솔에 붙여넣고 조회 버튼 클릭 후 payload 복사:")
            print('   (function(){')
            print('     const orig=XMLHttpRequest.prototype.send;')
            print('     XMLHttpRequest.prototype.send=function(body){')
            print('       if(body&&this._url&&this._url.includes("pubofrprogcom"))')
            print('         console.log("PAYLOAD:",body);')
            print('       return orig.apply(this,arguments);};')
            print('     const o2=XMLHttpRequest.prototype.open;')
            print('     XMLHttpRequest.prototype.open=function(m,u){this._url=u;return o2.apply(this,arguments);};')
            print('     console.log("인터셉터 등록 완료");')
            print('   })()')
            print()
            print("   복사 후: python scripts/ipo_bot.py debug-html --source progcom --raw-payload '<payload>'")
            return
        path = save_dir / "kind_progcom_raw.html"
        path.write_text(html, encoding="utf-8")
        print(f"✅ KIND 공모기업현황 HTML 저장: {path}")
        rows = _re.findall(r"<tr", html, _re.IGNORECASE)
        tds  = _re.findall(r"<td", html, _re.IGNORECASE)
        print(f"   <tr> 개수: {len(rows)}  /  <td> 개수: {tds and len(tds)}")
        # 첫 번째 데이터 행 컬럼 미리보기
        data_rows = _re.findall(r"<tr[^>]*>(.*?)</tr>", html, _re.DOTALL | _re.IGNORECASE)
        for row in data_rows:
            cells = _re.findall(r"<td[^>]*>(.*?)</td>", row, _re.DOTALL | _re.IGNORECASE)
            if len(cells) >= 5:
                texts = [_re.sub(r"<[^>]+>", "", c).strip()[:20] for c in cells]
                print(f"   샘플 행 ({len(cells)}열): {texts}")
                break
        print("   ⚠️  _parse_kind_progcom_html 컬럼 인덱스 확인 후 수정")

    else:  # kind (calendar)
        today = date.today()
        html = _kind_fetch_calendar(today.year, today.month)
        if not html:
            print("❌ KIND 캘린더 응답 없음")
            return
        path = save_dir / "kind_calendar_raw.html"
        path.write_text(html, encoding="utf-8")
        is_error = "페이지 오류" in html or "존재하지 않습니다" in html
        if is_error:
            print(f"⚠️  KIND 캘린더 에러 페이지 반환 ({path})")
        else:
            print(f"✅ KIND 캘린더 HTML 저장: {path}")
        rows = _re.findall(r"<tr", html, _re.IGNORECASE)
        print(f"   <tr> 개수: {len(rows)}")

    print(f"   파일을 열어 td 컬럼 순서 확인 후 파서 수정")


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="IPO봇 — 공모주 매력지수 스캐너")
    sub = ap.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="향후 N일 공모주 스캔")
    p_scan.add_argument("--days", type=int, default=30)
    p_scan.add_argument("--top",  type=int, default=10)
    p_scan.add_argument("--json", action="store_true", help="JSON 출력 (paper_ui용)")

    p_dbg = sub.add_parser("debug-html", help="38/KIND raw HTML 저장 (파서 개발용)")
    p_dbg.add_argument("--source", choices=["38", "kind", "progcom"], default="38")
    p_dbg.add_argument("--raw-payload", dest="raw_payload", default=None,
                       help="DevTools로 캡처한 POST body를 직접 주입 (progcom 디버깅용)")

    p_ana = sub.add_parser("analyze", help="단일 종목 수동 분석")
    p_ana.add_argument("corp_name")
    p_ana.add_argument("--rate",       type=float, help="수요예측 경쟁률")
    p_ana.add_argument("--band-low",   type=float, help="공모 희망가 하단")
    p_ana.add_argument("--band-high",  type=float, help="공모 희망가 상단")
    p_ana.add_argument("--price",      type=float, help="확정 공모가")
    p_ana.add_argument("--float",      type=float, help="유통 비율(%)")
    p_ana.add_argument("--lockup",     type=float, help="기관 의무보유 확약 비율(%)")
    p_ana.add_argument("--underwriter",            help="주관사명")
    p_ana.add_argument("--cap",        type=float, help="시총(억원)")

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "scan" or args.cmd is None:
        days = getattr(args, "days", 30)
        top  = getattr(args, "top",  10)
        use_json = getattr(args, "json", False)
        msg, items = run("scan", days_ahead=days, top_n=top)
        if use_json:
            import json as _json, dataclasses as _dc
            def _serial(obj):
                if isinstance(obj, dict):
                    d = dict(obj)
                elif _dc.is_dataclass(obj):
                    d = _dc.asdict(obj)
                elif hasattr(obj, "__dict__"):
                    d = dict(obj.__dict__)
                else:
                    return str(obj)
                # JS가 item.name으로 접근할 수 있도록 alias 추가
                if "corp_name" in d:
                    d.setdefault("name", d["corp_name"])
                if "band_high" in d:
                    d.setdefault("offer_band_high", d["band_high"])
                if "offer_amount" in d:
                    d.setdefault("offer_amount_100m", d["offer_amount"])
                return d
            print(_json.dumps(
                [_serial(it) for it in (items or [])],
                ensure_ascii=False, default=str
            ))
        else:
            print(msg)
    elif args.cmd == "analyze":
        msg, _ = analyze_manual(
            corp_name=args.corp_name,
            competition_rate=args.rate,
            band_low=args.band_low,
            band_high=args.band_high,
            final_price=args.price,
            float_ratio=getattr(args, "float", None),
            lockup_ratio=getattr(args, "lockup", None),
            underwriter=args.underwriter,
            offer_amount=args.cap,
        )
        print(msg)
    elif args.cmd == "debug-html":
        _cmd_debug_html(getattr(args, "source", "38"), raw_payload=getattr(args, "raw_payload", None))


if __name__ == "__main__":
    _cli()
