"""콴텍봇 v2 — 거시 국면 + 종목 팩터 스코어링 + 추천 (5단계 v3.23).

v3.22 (이미 구현):
- ECOS/FRED 시계열 fetch (CLI KR/US, BSI KR)
- CLI 모멘텀 산출 (최근 6M 평균 vs 직전 12M 평균)
- MSCI 4분면 국면 분류 (Recovery / Expansion / Slowdown / Contraction)
- 한국·미국·BSI 신호 일치도 기반 확신도 (0~1)

v3.23 (신규):
- wiki/투자/퀀트/국면별_팩터_가중.md 마크다운 표 파싱 → phase별 5팩터 가중치
- 5팩터 raw 점수 (Momentum / Value / Quality / Low Vol / Size)
  · Momentum:  12-1 J&T (kium_bot 동일 정의)  · OHLCV
  · Value:     1/PBR  · pykrx fundamental
  · Quality:   EPS/BPS (ROE 근사 — DART 정밀화는 v3.24+)  · pykrx fundamental
  · Low Vol:   -연환산 변동성 (낮을수록 매력)  · OHLCV
  · Size:      -log(시가총액) (소형주 알파)  · pykrx market_cap
- universe 안 z-score 정규화 + phase 가중합 → composite score
- recommend_top_n: phase별 Top N (Recovery/Expansion 8 / Slowdown 6 / Contraction 4)
- format_recommendations: 텔레그램/웹 UI 출력
- run("recommend", ...) 분기 추가
- 월간 리밸런싱 자동 푸시는 telegram_bot.py JobQueue로 통합

설계 결정:
- 가중치는 wiki에서 파싱 (사용자가 노트 직접 조정 → 코드 수정 없이 반영). 파싱 실패 시 코드 fallback.
- 결정론 z-score 가중합 — LLM 무호출, mode=fast (재현성·테스트 용이성)
- v3.21 빈 응답 캐시 가드 패턴 동일 적용

외부 의존:
- ECOS_API_KEY (CLI KR · BSI KR) — 거시 국면용
- FRED_API_KEY (CLI US) — 거시 국면용
- pykrx (KOSPI200 universe + ohlcv + fundamental + cap) — 팩터 스코어링용
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

from storage_paths import PATHS

log = logging.getLogger("quant_bot")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ─── 시리즈 카탈로그 ─────────────────────────────────

# ECOS — 한국은행 (12자리 stat_code · 8자리 item_code · cycle)
# v3.23.1 핫픽스 — CLI_KR을 FRED로 이동 (ECOS 시리즈 매핑 잘못. 사용자 환경 실측에서 발견).
#   - 기존 901Y027 = "경제활동인구" (CLI 아님) → FRED KORLOLITONOSTSAM 으로 이동
#   - 기존 BSI item_code "AX1AAA" 미존재 → "99988" (전산업, ECOS 512Y014 카탈로그 확인)
ECOS_SERIES = {
    "BSI_KR": {"stat_code": "512Y014", "item_code": "99988", "cycle": "M",
               "label": "한은 기업경기조사 BSI (전산업)"},
}

# FRED — St. Louis Fed
FRED_SERIES = {
    "CLI_US": {"series_id": "USALOLITONOSTSAM", "label": "미국 OECD CLI (정규화)"},
    # v3.23.1 — CLI_KR도 FRED로 통일. 한미 같은 OECD 시리즈 그룹 (CLI Normalised).
    "CLI_KR": {"series_id": "KORLOLITONOSTSAM", "label": "한국 OECD CLI (정규화)"},
}

# MSCI 4분면 라벨
PHASES = ("Recovery", "Expansion", "Slowdown", "Contraction")

# 시계열 캐시 (24h TTL — CLI는 월간 발표라 일일 재호출 불필요)
SERIES_TTL_SEC = 24 * 60 * 60
_SERIES_CACHE: dict[str, tuple[float, list[tuple[str, float]]]] = {}


# ─── HTTP 헬퍼 ────────────────────────────────────


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = Request(url, headers={"User-Agent": "quant_bot/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ─── ECOS 시계열 fetch ────────────────────────────


def _fetch_ecos_series_raw(stat_code: str, item_code: str, cycle: str, months: int) -> dict:
    """ECOS API — 시계열 N개월. ECOS_API_KEY 미설정 시 RuntimeError."""
    key = os.getenv("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY 미설정 (.env에 추가 필요)")
    today = datetime.now()
    if cycle == "M":
        # months개월치 안전 확보: months + 3 여유
        n = months + 3
        y = today.year - (n // 12)
        m = today.month - (n % 12)
        if m <= 0:
            y -= 1
            m += 12
        start = f"{y:04d}{m:02d}"
        end = today.strftime("%Y%m")
    else:
        raise ValueError(f"cycle 미지원: {cycle!r}")

    parts = [
        "https://ecos.bok.or.kr/api/StatisticSearch",
        quote(key, safe=""),
        "json", "kr", "1", "1000",
        quote(stat_code, safe=""),
        cycle, start, end,
        quote(item_code, safe=""),
    ]
    url = "/".join(parts)
    return _http_get_json(url)


def _parse_ecos_series(payload: dict) -> list[tuple[str, float]]:
    """ECOS 응답 → [(time, value)] 시간 오름차순. 데이터 없으면 빈 list."""
    rows = (payload.get("StatisticSearch") or {}).get("row") or []
    out: list[tuple[str, float]] = []
    for r in rows:
        t = r.get("TIME", "")
        v = r.get("DATA_VALUE", "")
        try:
            out.append((str(t), float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


# ─── FRED 시계열 fetch ────────────────────────────


def _fetch_fred_series_raw(series_id: str, months: int) -> dict:
    """FRED API — 최근 N개월. FRED_API_KEY 미설정 시 RuntimeError."""
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정 (.env에 추가 필요)")
    # months + 3 여유. CLI는 월간이라 한 달 1 row.
    limit = months + 3
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={quote(series_id, safe='')}"
        f"&api_key={quote(key, safe='')}"
        "&file_type=json"
        "&sort_order=desc"
        f"&limit={int(limit)}"
    )
    return _http_get_json(url)


def _parse_fred_series(payload: dict) -> list[tuple[str, float]]:
    """FRED 응답 → [(date, value)] 시간 오름차순. 데이터 없으면 빈 list."""
    obs = payload.get("observations") or []
    out: list[tuple[str, float]] = []
    for o in obs:
        d = o.get("date", "")
        v = o.get("value", "")
        if v in (".", "", None):
            continue
        try:
            out.append((str(d), float(v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


# ─── 통합 fetcher (캐시 포함) ─────────────────────


def fetch_series(name: str, months: int = 24) -> list[tuple[str, float]]:
    """name ∈ {CLI_KR, BSI_KR, CLI_US}. 24h 캐시. 빈 list 가드 — v3.21 패턴 적용."""
    cache_key = f"{name}_{months}"
    cached = _SERIES_CACHE.get(cache_key)
    if cached:
        ts, lst = cached
        if time.time() - ts < SERIES_TTL_SEC:
            return lst

    if name in ECOS_SERIES:
        spec = ECOS_SERIES[name]
        payload = _fetch_ecos_series_raw(spec["stat_code"], spec["item_code"], spec["cycle"], months)
        series = _parse_ecos_series(payload)
    elif name in FRED_SERIES:
        spec = FRED_SERIES[name]
        payload = _fetch_fred_series_raw(spec["series_id"], months)
        series = _parse_fred_series(payload)
    else:
        raise ValueError(f"알 수 없는 시리즈: {name!r}")

    # v3.21 패턴 — 빈 응답은 캐시하지 않는다
    if series:
        _SERIES_CACHE[cache_key] = (time.time(), series)
    else:
        log.warning(f"{name} fetch 빈 응답 — 캐시 skip (네트워크 오류 또는 시리즈 변경 의심)")
    return series


def clear_cache() -> None:
    """테스트·운영에서 캐시 강제 무효화."""
    _SERIES_CACHE.clear()


# ─── 지표 계산 ────────────────────────────────────


def compute_momentum(series: list[tuple[str, float]],
                     recent_n: int = 6, baseline_n: int = 12) -> float | None:
    """최근 recent_n개월 평균 vs 직전 baseline_n개월 평균의 차이.

    양수 = 가속, 음수 = 감속. 데이터 < (recent_n + baseline_n)이면 None.
    """
    if not series or len(series) < recent_n + baseline_n:
        return None
    values = [v for _, v in series]
    recent = values[-recent_n:]
    baseline = values[-(recent_n + baseline_n):-recent_n]
    return sum(recent) / len(recent) - sum(baseline) / len(baseline)


def compute_level(series: list[tuple[str, float]]) -> float | None:
    """최신 값. 빈 series면 None."""
    if not series:
        return None
    return series[-1][1]


def classify_phase(level: float | None, momentum: float | None,
                   level_threshold: float = 100.0) -> str | None:
    """MSCI 4분면 분류. level/momentum 둘 다 있어야 결정."""
    if level is None or momentum is None:
        return None
    above = level >= level_threshold
    accel = momentum >= 0
    if not above and accel:
        return "Recovery"
    if above and accel:
        return "Expansion"
    if above and not accel:
        return "Slowdown"
    return "Contraction"


# ─── 확신도 ───────────────────────────────────────


def compute_confidence(phase_kr: str | None, phase_us: str | None,
                       bsi_trend: float | None,
                       weight_kr: float = 0.4, weight_us: float = 0.4,
                       weight_bsi: float = 0.2) -> tuple[str | None, float]:
    """세 신호 일치도 → (대표 국면, 확신도 0~1).

    - 한국·미국 phase가 같으면 +0.4 + 0.4
    - BSI 추세(>0 가속, <0 감속)가 phase 모멘텀과 같은 부호면 +0.2
    - 의견 갈리면 우세한 phase 선택, 가중치만큼 확신도
    """
    if phase_kr is None and phase_us is None:
        return None, 0.0

    # 가중치 누적
    scores: dict[str, float] = {}
    if phase_kr is not None:
        scores[phase_kr] = scores.get(phase_kr, 0.0) + weight_kr
    if phase_us is not None:
        scores[phase_us] = scores.get(phase_us, 0.0) + weight_us
    if bsi_trend is not None and (phase_kr is not None or phase_us is not None):
        primary = phase_kr or phase_us
        # primary phase가 가속(Recovery/Expansion)인지 감속(Slowdown/Contraction)인지
        primary_accel = primary in ("Recovery", "Expansion")
        bsi_accel = bsi_trend >= 0
        if primary_accel == bsi_accel:
            scores[primary] = scores.get(primary, 0.0) + weight_bsi

    if not scores:
        return None, 0.0
    # Top1 / Top2
    sorted_scores = sorted(scores.items(), key=lambda x: -x[1])
    top1_phase, top1_score = sorted_scores[0]
    return top1_phase, round(top1_score, 3)


# ─── run() entrypoint ────────────────────────────


@dataclass
class PhaseSnapshot:
    phase_kr: str | None
    phase_us: str | None
    cli_kr_level: float | None
    cli_kr_momentum: float | None
    cli_us_level: float | None
    cli_us_momentum: float | None
    bsi_trend: float | None
    consensus_phase: str | None
    confidence: float
    needs_recheck: bool  # 확신도 < 0.6


def snapshot(months: int = 24) -> PhaseSnapshot:
    """현재 거시 국면 스냅샷. fetch 실패는 graceful (해당 신호만 None)."""
    cli_kr = []
    cli_us = []
    bsi_kr = []
    try:
        cli_kr = fetch_series("CLI_KR", months=months)
    except Exception as e:
        log.warning(f"CLI_KR fetch 실패: {e}")
    try:
        cli_us = fetch_series("CLI_US", months=months)
    except Exception as e:
        log.warning(f"CLI_US fetch 실패: {e}")
    try:
        bsi_kr = fetch_series("BSI_KR", months=months)
    except Exception as e:
        log.warning(f"BSI_KR fetch 실패: {e}")

    cli_kr_level = compute_level(cli_kr)
    cli_kr_momentum = compute_momentum(cli_kr)
    cli_us_level = compute_level(cli_us)
    cli_us_momentum = compute_momentum(cli_us)
    bsi_trend = compute_momentum(bsi_kr, recent_n=3, baseline_n=6)

    phase_kr = classify_phase(cli_kr_level, cli_kr_momentum)
    phase_us = classify_phase(cli_us_level, cli_us_momentum)

    consensus, confidence = compute_confidence(phase_kr, phase_us, bsi_trend)

    return PhaseSnapshot(
        phase_kr=phase_kr, phase_us=phase_us,
        cli_kr_level=cli_kr_level, cli_kr_momentum=cli_kr_momentum,
        cli_us_level=cli_us_level, cli_us_momentum=cli_us_momentum,
        bsi_trend=bsi_trend,
        consensus_phase=consensus, confidence=confidence,
        needs_recheck=confidence < 0.6 and consensus is not None,
    )


PHASE_EMOJI = {
    "Recovery": "🌱",
    "Expansion": "🚀",
    "Slowdown": "🌧️",
    "Contraction": "🧊",
}


def format_snapshot(snap: PhaseSnapshot) -> str:
    """텔레그램/웹UI 출력 텍스트."""
    lines = ["📊 콴텍봇 거시 국면 스냅샷 (MSCI 4분면)"]
    if snap.consensus_phase:
        emo = PHASE_EMOJI.get(snap.consensus_phase, "")
        lines.append(f"\n{emo} 통합 국면: **{snap.consensus_phase}** (확신도 {snap.confidence:.0%})")
        if snap.needs_recheck:
            lines.append("⚠️ 확신도 60% 미만 — 2주 재진단 필요. 직전 국면 가중 유지 권장.")
    else:
        lines.append("\n❌ 데이터 부족 — 국면 판단 불가 (ECOS/FRED 키 또는 네트워크 점검).")

    def _fmt_pair(label: str, level: float | None, momentum: float | None, phase: str | None) -> str:
        lev_s = f"{level:.2f}" if level is not None else "N/A"
        mom_s = f"{momentum:+.3f}" if momentum is not None else "N/A"
        emo = PHASE_EMOJI.get(phase, "")
        ph_s = f"{emo} {phase}" if phase else "(판단 불가)"
        return f"  {label}: level={lev_s} · 모멘텀={mom_s} → {ph_s}"

    lines.append("\n신호별:")
    lines.append(_fmt_pair("한국 CLI", snap.cli_kr_level, snap.cli_kr_momentum, snap.phase_kr))
    lines.append(_fmt_pair("미국 CLI", snap.cli_us_level, snap.cli_us_momentum, snap.phase_us))
    bsi_s = f"{snap.bsi_trend:+.3f}" if snap.bsi_trend is not None else "N/A"
    lines.append(f"  한은 BSI 추세: {bsi_s} (3M-6M 평균 차)")

    lines.append("\n💡 v3.22 v1 — 국면 판단까지. 종목 추천·리밸런싱은 v3.23+. 실주문 안 함.")
    return "\n".join(lines)


def run(action: str = "phase", **kwargs) -> tuple[str, list[dict]]:
    """라우터 entrypoint. action ∈ {phase, recommend}.

    Args:
        action: "phase" — 거시 국면만 (v3.22 기존)
                "recommend" — 거시 국면 + 종목 Top N 추천 (v3.23)
        months: 시계열 길이 (18~60 클램프, 기본 24)
        top_n: recommend 시 Top N (미지정 시 phase별 디폴트)
        market: "KOSPI200"|"KOSDAQ150"|"KOSPI200+KOSDAQ150" (기본 KOSPI200)
        phase_override: recommend 시 phase 직접 지정 (테스트·강제 분석용)

    Returns:
        (text, sources) — sources는 항상 빈 list (LLM 무호출 결정론).
    """
    valid_actions = {"phase", "recommend"}
    if action not in valid_actions:
        return f"❌ 알 수 없는 action: {action!r} (지원: {sorted(valid_actions)})", []

    months = int(kwargs.get("months", 24))
    if months < 18:
        months = 18
    if months > 60:
        months = 60

    try:
        snap = snapshot(months=months)
    except Exception as e:
        log.error(f"snapshot 실패: {e}")
        return f"❌ 콴텍봇 snapshot 실패: {e}", []

    if action == "phase":
        return format_snapshot(snap), []

    # action == "recommend"
    phase_override = kwargs.get("phase_override")
    phase = phase_override if phase_override in PHASES else snap.consensus_phase
    if not phase:
        return (
            format_snapshot(snap)
            + "\n\n❌ 종목 추천 불가 — 거시 국면 미확정. ECOS/FRED 키 또는 네트워크 확인.",
            [],
        )

    market = (kwargs.get("market") or "KOSPI200").strip()
    top_n_arg = kwargs.get("top_n")
    try:
        top_n = int(top_n_arg) if top_n_arg is not None else None
    except (TypeError, ValueError):
        top_n = None

    try:
        weights_all = parse_phase_weights_from_wiki()
        recs = recommend_top_n(
            phase=phase, market=market, top_n=top_n, weights=weights_all,
        )
    except Exception as e:
        log.exception("recommend 실패")
        return (
            format_snapshot(snap) + f"\n\n❌ 콴텍봇 recommend 실패: {e}",
            [],
        )

    text = format_recommendations(
        phase=phase, recommendations=recs,
        confidence=snap.confidence,
        needs_recheck=snap.needs_recheck,
        weights=weights_all.get(phase),
        snapshot_text=format_snapshot(snap),
    )
    return text, []



# ═══════════════════════════════════════════════════════════════
# v3.23 — 종목 팩터 스코어링 + Top N 추천
# ═══════════════════════════════════════════════════════════════


# ─── Phase 가중치 wiki 파싱 ──────────────────────

# 종목 추천에서 사용하는 5팩터 (Cash/Bond, Growth는 별도 외부 운용)
SCORING_FACTORS: tuple[str, ...] = ("Momentum", "Value", "Quality", "LowVol", "Size")

# wiki 파싱 실패 시 fallback (Cash/Bond·Growth 제외 — 종목 점수만)
PHASE_FACTOR_WEIGHTS_FALLBACK: dict[str, dict[str, float]] = {
    "Recovery":    {"Momentum": 0.30, "Value": 0.25, "Quality": 0.15, "LowVol": 0.10, "Size": 0.20},
    "Expansion":   {"Momentum": 0.40, "Value": 0.10, "Quality": 0.15, "LowVol": 0.00, "Size": 0.10},
    "Slowdown":    {"Momentum": 0.10, "Value": 0.20, "Quality": 0.35, "LowVol": 0.30, "Size": 0.05},
    "Contraction": {"Momentum": 0.00, "Value": 0.10, "Quality": 0.30, "LowVol": 0.40, "Size": 0.00},
}

# phase별 Top N 디폴트 (wiki 권고 — 사용자 override 가능)
PHASE_TOP_N_DEFAULT: dict[str, int] = {
    "Recovery": 8, "Expansion": 8, "Slowdown": 6, "Contraction": 4,
}

# wiki 노트 경로 (사용자가 wiki에서 직접 조정 가능)
WIKI_FACTOR_NOTE_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "obsidian-vault" / "wiki" / "투자" / "퀀트" / "국면별_팩터_가중.md"
)


def _normalize_factor_name(raw: str) -> str | None:
    """wiki 표의 한글/영어 혼합 → 코드 표준 SCORING_FACTORS."""
    n = raw.strip().lower()
    if not n:
        return None
    if "momentum" in n or "모멘텀" in n:
        return "Momentum"
    if "value" in n or "가치" in n or "pbr" in n or "per" in n:
        return "Value"
    if "quality" in n or "퀄리티" in n or "roe" in n:
        return "Quality"
    if ("low" in n and "vol" in n) or "저변동" in n or "낮은 변동" in n:
        return "LowVol"
    if "size" in n or "소형" in n:
        return "Size"
    return None


def _normalize_phase_weights(d: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """SCORING_FACTORS 5개에 대해서만 weight 추출 + 합으로 정규화 → 합 1.0."""
    out: dict[str, dict[str, float]] = {}
    for phase, weights in d.items():
        sub = {f: float(weights.get(f, 0.0)) for f in SCORING_FACTORS}
        s = sum(sub.values())
        if s > 0:
            sub = {f: v / s for f, v in sub.items()}
        out[phase] = sub
    return out


def parse_phase_weights_from_wiki(path: Path | None = None) -> dict[str, dict[str, float]]:
    """wiki 노트에서 4분면×5팩터 가중치 파싱.

    매칭 규칙:
      `#### Recovery — ...` 같은 헤딩 → 다음 헤딩 직전까지가 phase 섹션
      섹션 안 마크다운 표 행 `| 팩터 | 가중 | ... |` → 첫 컬럼 팩터, 둘째 컬럼 weight
      팩터 이름은 한/영 혼용 — _normalize_factor_name 매핑

    실패·노트 부재·일부 phase 결측 → fallback에서 보충. 최종 합 1.0 정규화.
    """
    target = path or WIKI_FACTOR_NOTE_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"wiki 가중치 노트 읽기 실패 — fallback 사용: {e}")
        return _normalize_phase_weights(PHASE_FACTOR_WEIGHTS_FALLBACK)

    section_re = re.compile(
        r"####\s+(Recovery|Expansion|Slowdown|Contraction)\b[^\n]*\n",
        re.IGNORECASE,
    )
    rows = list(section_re.finditer(text))
    if not rows:
        log.warning("wiki 가중치 노트에 phase 섹션 헤딩 없음 — fallback")
        return _normalize_phase_weights(PHASE_FACTOR_WEIGHTS_FALLBACK)

    out: dict[str, dict[str, float]] = {}
    for i, m in enumerate(rows):
        phase = m.group(1).capitalize()
        sec_start = m.end()
        sec_end = rows[i + 1].start() if i + 1 < len(rows) else len(text)
        section = text[sec_start:sec_end]

        weights: dict[str, float] = {}
        for ln in section.splitlines():
            ln = ln.strip()
            if not ln.startswith("|") or "---" in ln:
                continue
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) < 2:
                continue
            factor = _normalize_factor_name(cells[0])
            if not factor:
                continue
            try:
                weights[factor] = float(cells[1])
            except ValueError:
                continue
        if weights:
            out[phase] = weights

    # 누락 phase는 fallback에서
    for phase, fb in PHASE_FACTOR_WEIGHTS_FALLBACK.items():
        if phase not in out:
            out[phase] = dict(fb)
    return _normalize_phase_weights(out)


# ─── pykrx fundamental + market cap ─────────────


def _fetch_fundamental_raw(date: str, market: str = "KOSPI"):
    """pykrx 한 시장 전체 PBR/PER/EPS/BPS/DPS/DIV. 외부 호출."""
    from pykrx import stock
    return stock.get_market_fundamental_by_ticker(date, market=market)


def _fetch_cap_raw(date: str, market: str = "KOSPI"):
    """pykrx 한 시장 전체 시가총액·거래대금·상장주식수. 외부 호출."""
    from pykrx import stock
    return stock.get_market_cap_by_ticker(date, market=market)


def fetch_fundamentals(market: str = "KOSPI") -> dict[str, dict[str, float]]:
    """ticker → {BPS, PER, PBR, EPS, DPS, DIV}. 실패 시 빈 dict."""
    today = datetime.now().strftime("%Y%m%d")
    try:
        df = _fetch_fundamental_raw(today, market=market)
    except Exception as e:
        log.warning(f"fundamental fetch 실패 ({market}): {e}")
        return {}
    out: dict[str, dict[str, float]] = {}
    if df is None or len(df) == 0:
        return out
    for ticker in df.index:
        try:
            row = df.loc[ticker]
            out[str(ticker)] = {
                "BPS": float(row.get("BPS", 0) or 0),
                "PER": float(row.get("PER", 0) or 0),
                "PBR": float(row.get("PBR", 0) or 0),
                "EPS": float(row.get("EPS", 0) or 0),
                "DPS": float(row.get("DPS", 0) or 0),
                "DIV": float(row.get("DIV", 0) or 0),
            }
        except Exception:
            continue
    return out


def fetch_market_caps(market: str = "KOSPI") -> dict[str, float]:
    """ticker → 시가총액(원). 실패 시 빈 dict."""
    today = datetime.now().strftime("%Y%m%d")
    try:
        df = _fetch_cap_raw(today, market=market)
    except Exception as e:
        log.warning(f"market cap fetch 실패 ({market}): {e}")
        return {}
    out: dict[str, float] = {}
    if df is None or len(df) == 0:
        return out
    for ticker in df.index:
        try:
            row = df.loc[ticker]
            cap = float(row.get("시가총액", 0) or 0)
            if cap > 0:
                out[str(ticker)] = cap
        except Exception:
            continue
    return out


# ─── OHLCV 디스크 캐시 (v3.23.2) ───────────────
# universe 200종목 × OHLCV 순차 fetch가 3~6분 병목. 24h 디스크 캐시로
# 두 번째 호출부터 ~5초로 단축. v3.21 빈 응답 가드 패턴 적용.

OHLCV_TTL_SEC = 24 * 60 * 60
_OHLCV_CACHE_DIR: Path = PATHS.shareable_cache_dir / "ohlcv"


def _ohlcv_cache_path(ticker: str, end_date: str) -> Path:
    return _OHLCV_CACHE_DIR / f"{ticker}_{end_date}.json"


def _load_ohlcv_cache(ticker: str, end_date: str):
    """ticker × end_date 캐시 hit → close pd.Series (DataFrame['종가'])로 복원. miss → None."""
    p = _ohlcv_cache_path(ticker, end_date)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime >= OHLCV_TTL_SEC:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("close"):
            return None
        import pandas as pd  # 지연 import
        return pd.DataFrame({"종가": [float(x) for x in data["close"]]})
    except Exception as e:
        log.debug(f"OHLCV 캐시 로드 실패 {ticker}: {e}")
        return None


def _save_ohlcv_cache(ticker: str, end_date: str, close_series) -> None:
    """close pd.Series/list → 디스크 캐시. 빈 응답·예외 시 skip (v3.21 패턴)."""
    if close_series is None:
        return
    try:
        if hasattr(close_series, "tolist"):
            close_list = [float(x) for x in close_series.tolist()]
        else:
            close_list = [float(x) for x in close_series]
        if not close_list:
            log.debug(f"OHLCV 캐시 skip — 빈 응답 ({ticker})")
            return
        _OHLCV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _ohlcv_cache_path(ticker, end_date)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"close": close_list}), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        log.debug(f"OHLCV 캐시 저장 실패 {ticker}: {e}")


def fetch_ohlcv_cached(ticker: str, start: str, end: str):
    """OHLCV with 24h 디스크 캐시. 외부 fetcher는 kium_bot._fetch_ohlcv_raw 재사용.

    캐시 hit이면 close만 담은 DataFrame 반환 (다른 컬럼 필요 없음 — v3.23 팩터 점수는
    close만 사용. low_vol·momentum 모두 close 기반).
    """
    cached = _load_ohlcv_cache(ticker, end)
    if cached is not None:
        return cached
    try:
        from kium_bot import _fetch_ohlcv_raw
        df = _fetch_ohlcv_raw(ticker, start, end)
    except Exception as e:
        log.debug(f"{ticker} ohlcv fetch 실패: {e}")
        return None
    if df is None or len(df) == 0:
        return None
    close = None
    for col in ("종가", "Close", "close"):
        if hasattr(df, "columns") and col in df.columns:
            close = df[col]
            break
    if close is not None:
        _save_ohlcv_cache(ticker, end, close)
    return df


# ─── 5팩터 raw 점수 ────────────────────────────


def compute_momentum_from_prices(
    prices, lookback_days: int = 252, skip_days: int = 21,
) -> float | None:
    """12-1 J&T 모멘텀 (kium_bot.compute_momentum_score 동일 정의 — 의존성 분리용)."""
    try:
        n = len(prices)
    except TypeError:
        return None
    if n < lookback_days + 1:
        return None
    if skip_days < 0 or lookback_days <= skip_days:
        return None

    def at(i: int) -> float:
        if hasattr(prices, "iloc"):
            return float(prices.iloc[i])
        return float(prices[i])

    p_recent = at(-skip_days - 1)
    p_old = at(-lookback_days - 1)
    if p_old <= 0:
        return None
    return (p_recent / p_old) - 1.0


def compute_factor_scores(
    close,
    fundamental: dict | None = None,
    market_cap: float | None = None,
    *,
    momentum_lookback: int = 252,
    momentum_skip: int = 21,
    vol_window: int = 63,
) -> dict[str, float | None]:
    """단일 종목 5팩터 raw 점수. None은 결측 (z-score 단계에서 제외)."""
    out: dict[str, float | None] = {f: None for f in SCORING_FACTORS}

    # Momentum — 12-1
    out["Momentum"] = compute_momentum_from_prices(close, momentum_lookback, momentum_skip)

    # Low Vol — 음수 부호 (z-score에서 클수록 매력)
    if close is not None:
        if hasattr(close, "iloc") and len(close) >= vol_window + 1:
            try:
                rets = close.pct_change().dropna()
                if len(rets) >= vol_window:
                    std = float(rets.iloc[-vol_window:].std())
                    if std > 0:
                        ann_vol = std * (252 ** 0.5)
                        out["LowVol"] = -ann_vol
            except Exception:
                pass

    # Value — 1/PBR
    if fundamental:
        pbr = float(fundamental.get("PBR", 0) or 0)
        if pbr > 0:
            out["Value"] = 1.0 / pbr

    # Quality — EPS / BPS (ROE 근사)
    if fundamental:
        bps = float(fundamental.get("BPS", 0) or 0)
        eps = float(fundamental.get("EPS", 0) or 0)
        if bps > 0:
            out["Quality"] = eps / bps

    # Size — -log(시가총액)
    if market_cap is not None and market_cap > 0:
        out["Size"] = -math.log(float(market_cap))

    return out


# ─── z-score 정규화 + 가중합 ──────────────────


def z_score_normalize(values: list) -> list:
    """None 유지. 표준편차 0 또는 valid<2면 0.0."""
    valid = [v for v in values if v is not None]
    if not valid:
        return list(values)
    if len(valid) < 2:
        return [0.0 if v is not None else None for v in values]
    mean = sum(valid) / len(valid)
    var = sum((v - mean) ** 2 for v in valid) / len(valid)
    std = var ** 0.5
    if std <= 0:
        return [0.0 if v is not None else None for v in values]
    return [((v - mean) / std) if v is not None else None for v in values]


def combine_factor_scores(
    z_scores: dict[str, float | None], weights: dict[str, float],
) -> float | None:
    """가중합. 결측 팩터는 가중치 재정규화 후 합산. 모두 결측이면 None."""
    used: list[tuple[float, float]] = []
    for f in SCORING_FACTORS:
        z = z_scores.get(f)
        w = float(weights.get(f, 0.0))
        if z is None or w <= 0:
            continue
        used.append((float(z), w))
    if not used:
        return None
    total_w = sum(w for _, w in used)
    if total_w <= 0:
        return None
    return sum(z * w for z, w in used) / total_w


# ─── recommend_top_n 통합 ─────────────────────


@dataclass
class StockRecommendation:
    ticker: str
    name: str
    composite_score: float
    raw_factors: dict
    z_factors: dict
    current_price: float | None


def recommend_top_n(
    phase: str,
    universe: list | None = None,
    market: str = "KOSPI200",
    top_n: int | None = None,
    weights: dict | None = None,
    fundamentals: dict | None = None,
    market_caps: dict | None = None,
    *,
    momentum_lookback: int = 252,
    momentum_skip: int = 21,
    vol_window: int = 63,
) -> list[StockRecommendation]:
    """phase별 Top N 종목 추천 (결정론 z-score 가중합).

    Args:
      phase: Recovery / Expansion / Slowdown / Contraction
      universe: 직접 주입(테스트용). None이면 kium_bot.fetch_universe(market).
      market: KOSPI200 / KOSDAQ150 / KOSPI200+KOSDAQ150
      top_n: 미지정 시 phase별 디폴트 (Recovery/Expansion 8 / Slowdown 6 / Contraction 4)
      weights: 미지정 시 wiki 파싱 → fallback (PHASE_FACTOR_WEIGHTS_FALLBACK)
      fundamentals/market_caps: 직접 주입(테스트용). None이면 pykrx fetch.

    Returns:
      Top N StockRecommendation. universe 비었거나 phase 모르면 빈 list.
    """
    if phase not in PHASES:
        return []

    if universe is None:
        try:
            from kium_bot import fetch_universe
            universe = fetch_universe(market=market)
        except Exception as e:
            log.warning(f"universe fetch 실패: {e}")
            return []
    if not universe:
        return []

    if weights is None:
        weights = parse_phase_weights_from_wiki()
    phase_weights = weights.get(phase, {})
    if not phase_weights or sum(phase_weights.values()) <= 0:
        log.warning(f"phase '{phase}' 가중치 0 — 추천 불가")
        return []

    if top_n is None:
        top_n = PHASE_TOP_N_DEFAULT.get(phase, 8)
    top_n = max(1, int(top_n))

    if fundamentals is None:
        fundamentals = fetch_fundamentals()
    if market_caps is None:
        market_caps = fetch_market_caps()

    today = datetime.now()
    start_date = (
        today - timedelta(days=int(momentum_lookback * 1.6) + 30)
    ).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    raw_results: list[tuple[str, str, dict, float | None]] = []
    n_universe = len(universe)
    log.info(
        f"recommend OHLCV fetch 시작 (universe={n_universe}, "
        "캐시 miss 시 병렬 fetch — 최대 2~3분)"
    )

    def _score_one(args_tuple):
        """단일 종목 OHLCV fetch + 팩터 계산.
        캐시 hit: 즉시 반환. 캐시 miss: 3초 시도 후 skip (응답성 우선).
        """
        ticker, name = args_tuple
        # 캐시 hit이면 네트워크 없이 즉시 반환
        cached = _load_ohlcv_cache(ticker, end_date)
        if cached is not None:
            df = cached
        else:
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(fetch_ohlcv_cached, ticker, start_date, end_date)
                try:
                    df = fut.result(timeout=3)   # miss면 3초만 대기
                except _cf.TimeoutError:
                    log.debug(f"{ticker} ohlcv timeout (3s) — skip")
                    return None
        if df is None or len(df) < momentum_lookback + 1:
            return None
        close = None
        for col in ("종가", "Close", "close"):
            if hasattr(df, "columns") and col in df.columns:
                close = df[col]
                break
        if close is None:
            return None
        try:
            current = float(close.iloc[-1])
        except Exception:
            current = None
        factors = compute_factor_scores(
            close,
            fundamental=fundamentals.get(ticker),
            market_cap=market_caps.get(ticker),
            momentum_lookback=momentum_lookback,
            momentum_skip=momentum_skip,
            vol_window=vol_window,
        )
        return (str(ticker), str(name), factors, current)

    # pykrx는 GIL 구간이 있어 workers=8 이상은 효과 감소 — 8로 제한
    import concurrent.futures as _cf
    workers = min(8, n_universe)
    with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_score_one, pair): pair for pair in universe}
        done = 0
        for fut in _cf.as_completed(futs):
            done += 1
            if done % 25 == 0:
                log.info(f"  진행 {done}/{n_universe} ({100*done/n_universe:.0f}%)")
            result = fut.result()
            if result is not None:
                raw_results.append(result)

    log.info(f"OHLCV fetch 완료 — {len(raw_results)}/{n_universe} 종목 점수 산출 가능")
    if not raw_results:
        log.info("recommend: raw_results 비어있음 — universe ohlcv 부족 추정")
        return []

    # universe 안 z-score 정규화
    z_arrays: dict[str, list] = {}
    for f in SCORING_FACTORS:
        z_arrays[f] = z_score_normalize([r[2].get(f) for r in raw_results])

    scored: list[StockRecommendation] = []
    for i, (ticker, name, raw_f, cur) in enumerate(raw_results):
        z_f = {f: z_arrays[f][i] for f in SCORING_FACTORS}
        comp = combine_factor_scores(z_f, phase_weights)
        if comp is None:
            continue
        scored.append(StockRecommendation(
            ticker=ticker, name=name,
            composite_score=comp,
            raw_factors=dict(raw_f),
            z_factors=z_f,
            current_price=cur,
        ))
    scored.sort(key=lambda r: r.composite_score, reverse=True)
    return scored[:top_n]


def format_recommendations(
    phase: str,
    recommendations: list,
    *,
    confidence: float | None = None,
    needs_recheck: bool = False,
    weights: dict | None = None,
    snapshot_text: str | None = None,
) -> str:
    """텔레그램/웹 UI 출력 텍스트. snapshot_text 있으면 헤더에 첨부."""
    emoji = PHASE_EMOJI.get(phase, "")
    lines: list[str] = []
    if snapshot_text:
        lines.append(snapshot_text)
        lines.append("")
        lines.append("─" * 30)
        lines.append("")
    lines.append(f"📈 콴텍봇 종목 추천 — {emoji} {phase} (Top {len(recommendations)})")
    if confidence is not None:
        suffix = " ⚠️ 재진단 권고" if needs_recheck else ""
        lines.append(f"   확신도 {confidence:.0%}{suffix}")
    if weights:
        wstr = " · ".join(
            f"{f}={weights.get(f, 0):.2f}"
            for f in SCORING_FACTORS if weights.get(f, 0) > 0
        )
        if wstr:
            lines.append(f"   가중: {wstr}")
    lines.append(
        "⚠️ 생존편향 주의 (현재 상장 종목만) — 6개월 paper trading 검증 후 KIS"
    )
    lines.append("")
    if not recommendations:
        lines.append("(결과 없음 — universe·가중치·OHLCV 데이터 확인 필요)")
        return "\n".join(lines)
    for i, r in enumerate(recommendations, 1):
        cur_s = f"{r.current_price:,.0f}원" if r.current_price else "N/A"
        lines.append(
            f"{i:>2}. {r.name} ({r.ticker})  "
            f"점수 {r.composite_score:+.2f}  현재 {cur_s}"
        )
        parts: list[str] = []
        for f in SCORING_FACTORS:
            z = r.z_factors.get(f)
            if z is not None:
                parts.append(f"{f[:3]} {z:+.1f}")
        if parts:
            lines.append("     " + " · ".join(parts))
    if phase == "Contraction":
        lines.append("")
        lines.append(
            "💡 Contraction — 채권 ETF 50% 권고 "
            "(KODEX 국고채3년 / TIGER 단기통안채)"
        )
    lines.append("")
    lines.append(
        "💡 v3.23 v2 — 결정론 z-score 가중합. 실주문 안 함. "
        "매월 첫 영업일 09:30 자동 푸시."
    )
    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────


def _cli() -> None:
    # CLI 직접 실행 시 .env 자동 로드 (텔레그램/웹 UI에서 호출될 땐 이미 환경에 있음).
    # 봇 라이브러리(load_dotenv) 없으면 silent skip.
    try:
        from dotenv import load_dotenv  # type: ignore
        from pathlib import Path
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass

    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("action", nargs="?", default="phase",
                   choices=["phase", "recommend"])
    p.add_argument("--months", type=int, default=24)
    p.add_argument("--top-n", type=int, default=None,
                   help="recommend 시 Top N (미지정 시 phase별 디폴트)")
    p.add_argument("--market", default="KOSPI200")
    p.add_argument("--phase-override", default=None,
                   choices=[None, "Recovery", "Expansion", "Slowdown", "Contraction"])
    args = p.parse_args()
    text, _ = run(
        args.action, months=args.months,
        top_n=args.top_n, market=args.market,
        phase_override=args.phase_override,
    )
    print(text.replace("\\n", "\n"))


if __name__ == "__main__":
    _cli()
