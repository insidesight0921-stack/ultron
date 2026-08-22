#!/usr/bin/env python3
"""
weekly_kium_scan.py — 키움봇 v3 주간 자동 스캔 러너 (v3.24)

매주 월요일 09:00 launchd가 자동 실행.
흐름:
  1. kium_bot.run(action="scan", with_crash_signals=True)
  2. HTML + TXT 리포트 → ``storage_paths``의 Private reports
  3. 텔레그램 푸시 (TELEGRAM_BOT_TOKEN + ALLOWED_TELEGRAM_USER_ID)
  4. Slack 웹훅 푸시 (SLACK_WEBHOOK_URL 환경변수 있을 때만)

수동 실행 예시:
  python scripts/weekly_kium_scan.py
  python scripts/weekly_kium_scan.py --top 20 --market KOSPI200+KOSDAQ150
  python scripts/weekly_kium_scan.py --dry-run    # 저장·푸시 없이 출력만
  python scripts/weekly_kium_scan.py --no-crash   # crash_signals 없이 빠르게
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ─── 경로 설정 ───────────────────────────────────────
HOME = Path.home()
PROJECT = HOME / "울트론" / "ai-agent"
SCRIPTS = PROJECT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from storage_paths import PATHS  # noqa: E402

DATA_DIR = PATHS.private_root
REPORT_DIR = PATHS.reports_dir
LOG_DIR = PATHS.logs_dir


# .env 로드 (python-dotenv 없어도 동작)
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv(PROJECT / ".env")

# ─── 로깅 설정 ───────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "weekly_kium.out.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("weekly_kium_scan")

# WARNING 이상 → err 로그에도 기록
err_handler = logging.FileHandler(LOG_DIR / "weekly_kium.err.log", encoding="utf-8")
err_handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(err_handler)

# ─── 모듈 import ─────────────────────────────────────
try:
    import kium_bot
except ImportError as e:
    log.error(f"kium_bot import 실패: {e}")
    sys.exit(1)


# ─── 텔레그램 푸시 ──────────────────────────────────

def _tg_send(token: str, chat_id: str, text: str) -> bool:
    """Telegram Bot API sendMessage. 4000자 초과 시 자동 분할."""
    MAX = 4000
    chunks = [text[i : i + MAX] for i in range(0, len(text), MAX)]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in chunks:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode())
                if not result.get("ok"):
                    log.warning(f"텔레그램 응답 오류: {result}")
                    ok = False
        except Exception as exc:
            log.error(f"텔레그램 전송 실패 (chat_id={chat_id}): {exc}")
            ok = False
    return ok


def push_telegram(text: str) -> int:
    """등록된 모든 user_id에게 푸시. 성공 건수 반환."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_ids = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
    if not token or not raw_ids:
        log.warning("텔레그램 환경변수 없음 — 푸시 스킵")
        return 0
    chat_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
    sent = 0
    for cid in chat_ids:
        if _tg_send(token, cid, text):
            log.info(f"✅ 텔레그램 푸시 완료: chat_id={cid}")
            sent += 1
    return sent


# ─── Slack 웹훅 푸시 ────────────────────────────────

def push_slack(text: str) -> bool:
    """SLACK_WEBHOOK_URL 환경변수가 있을 때만 동작."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
            if body.strip() == "ok":
                log.info("✅ Slack 푸시 완료")
                return True
            log.warning(f"Slack 응답 이상: {body!r}")
            return False
    except Exception as exc:
        log.error(f"Slack 푸시 실패: {exc}")
        return False


# ─── HTML 리포트 생성 ────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>키움봇 주간 모멘텀 스캔 — {date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Pretendard', 'Apple SD Gothic Neo', sans-serif;
          background: #f7f8fa; color: #1a1a2e; padding: 24px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 28px;
           box-shadow: 0 2px 12px rgba(0,0,0,.08); max-width: 960px;
           margin: 0 auto 20px; }}
  h1 {{ font-size: 1.5rem; color: #1a1a2e; margin-bottom: 6px; }}
  .meta {{ font-size: .85rem; color: #888; margin-bottom: 16px; }}
  .warn {{ color: #c0392b; }}
  .signal-box {{ border-radius: 8px; padding: 16px; margin-bottom: 16px;
                 font-size: .92rem; line-height: 1.8; }}
  .signal-ok   {{ background: #e8f9f0; border-left: 4px solid #2dc653; }}
  .signal-warn {{ background: #fff4e5; border-left: 4px solid #f5a623; }}
  .signal-crit {{ background: #fde8e8; border-left: 4px solid #e53e3e; }}
  .weight-box {{ background: #eef2ff; border-radius: 8px; padding: 14px;
                 font-size: .92rem; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th {{ background: #f0f2ff; text-align: left; padding: 10px 12px;
        font-weight: 600; color: #3b4cca; border-bottom: 2px solid #dde; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #eee; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fafbff; }}
  .rank {{ font-weight: 700; color: #3b4cca; text-align: center; }}
  .pos {{ color: #2dc653; font-weight: 600; }}
  .neg {{ color: #e53e3e; font-weight: 600; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: .78rem; font-weight: 600; margin-right: 4px; }}
  .badge-hit {{ background: #fde8e8; color: #e53e3e; }}
  .badge-ok  {{ background: #e8f9f0; color: #2dc653; }}
  footer {{ text-align: center; font-size: .78rem; color: #bbb; margin-top: 24px; }}
</style>
</head>
<body>
<div class="card">
  <h1>📊 키움봇 주간 모멘텀 스캔 (12-1 J&amp;T)</h1>
  <div class="meta">기준일: {date} &nbsp;|&nbsp; 유니버스: {market} &nbsp;|&nbsp; 생성: {generated_at}</div>
  <div class="meta warn">⚠️ 생존편향 주의 — 현재 상장 종목 기준 (1~2%p 과대 추정 가능)</div>
  {crash_html}
  {weight_html}
  <table>
    <thead>
      <tr>
        <th style="width:48px">순위</th>
        <th>종목명 (티커)</th>
        <th>12-1 점수</th>
        <th>최근 1M</th>
        <th>최근 12M</th>
        <th>현재가</th>
      </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
</div>
<footer>키움봇 v3.24 · 매주 월요일 09:00 자동 생성 · 투자 결정 참고용 (정보 제공 목적)</footer>
</body>
</html>"""


def _pct_html(val: float | None, default: str = "N/A") -> str:
    if val is None:
        return default
    pct = val * 100
    cls = "pos" if pct >= 0 else "neg"
    return f'<span class="{cls}">{pct:+.1f}%</span>'


def _signal_html(crash_signals: dict | None) -> str:
    if not crash_signals:
        return ""
    hits = crash_signals.get("hits", 0)
    rec = crash_signals.get("recommendation", "")
    cls = "signal-crit" if hits >= 2 else ("signal-warn" if hits == 1 else "signal-ok")

    vol = crash_signals.get("vol_spike", {})
    panic = crash_signals.get("market_panic", {})
    rev = crash_signals.get("momentum_reversal", {})

    def _row(sig: dict, label: str, val_str: str) -> str:
        if sig.get("hit"):
            badge = '<span class="badge badge-hit">HIT</span>'
        else:
            badge = '<span class="badge badge-ok">✓</span>'
        return f"      <li>{badge}{label}: {val_str}</li>"

    ratio = vol.get("ratio")
    ret_w = panic.get("return_window")
    avg = rev.get("avg_return_1m")
    items = "\n".join([
        _row(vol,   "변동성 급등",
             f"21d/63d ratio = {ratio:.2f}" if ratio is not None else "데이터 부족"),
        _row(panic, "시장 패닉 (KOSPI)",
             f"20일 수익률 = {ret_w * 100:+.1f}%" if ret_w is not None else "데이터 부족"),
        _row(rev,   "모멘텀 내부 역전",
             f"Top 평균 1M = {avg * 100:+.1f}%" if avg is not None else "데이터 부족"),
    ])
    return (
        f'  <div class="{cls} signal-box">\n'
        f"    <strong>{rec}</strong>\n"
        f'    <ul style="margin-top:8px;padding-left:0;list-style:none">\n'
        f"{items}\n"
        f"    </ul>\n"
        f"  </div>\n"
    )


def _weight_html(weight: dict | None) -> str:
    if not weight:
        return ""
    eq = (weight.get("equity_weight") or 0) * 100
    bd = (weight.get("bond_weight") or 0) * 100
    reason = weight.get("reason", "")
    return (
        f'  <div class="weight-box">\n'
        f"    <strong>📐 권장 비중:</strong> 주식 {eq:.0f}% / 채권 {bd:.0f}%"
        f" &nbsp;|&nbsp; {reason}\n"
        f"  </div>\n"
    )


def _table_rows(results: list[dict]) -> str:
    rows = []
    for i, r in enumerate(results, 1):
        name = r.get("name", "")
        ticker = r.get("ticker", "")
        cur = r.get("current_price") or 0
        row = (
            f'      <tr>'
            f'<td class="rank">{i}</td>'
            f"<td>{name} ({ticker})</td>"
            f"<td>{_pct_html(r.get('score'))}</td>"
            f"<td>{_pct_html(r.get('return_1m'))}</td>"
            f"<td>{_pct_html(r.get('return_12m'))}</td>"
            f"<td>{cur:,.0f}원</td>"
            f"</tr>"
        )
        rows.append(row)
    return "\n".join(rows)


def make_html_report(
    results: list[dict],
    crash_signals: dict | None,
    weight: dict | None,
    market: str,
) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d (%a)")
    gen_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _HTML_TEMPLATE.format(
        date=date_str,
        market=market,
        generated_at=gen_str,
        crash_html=_signal_html(crash_signals),
        weight_html=_weight_html(weight),
        rows=_table_rows(results),
    )


# ─── 리포트 저장 ────────────────────────────────────

def save_reports(text: str, html: str, date_str: str) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = REPORT_DIR / f"kium_weekly_{date_str}.txt"
    html_path = REPORT_DIR / f"kium_weekly_{date_str}.html"
    txt_path.write_text(text, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    log.info(f"리포트 저장: {txt_path.name}")
    log.info(f"리포트 저장: {html_path.name}")
    return txt_path, html_path


# ─── 메인 ────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="키움봇 v3 주간 스캔 러너 (v3.24)")
    ap.add_argument("--top",      type=int, default=20,       help="Top N 종목 (기본 20)")
    ap.add_argument("--market",   default="KOSPI200",         help="유니버스 (기본 KOSPI200)")
    ap.add_argument("--no-crash", action="store_true",        help="crash_signals 계산 스킵")
    ap.add_argument("--dry-run",  action="store_true",        help="저장·푸시 없이 출력만")
    args = ap.parse_args()

    date_str = datetime.now().strftime("%Y%m%d")
    log.info(f"=== 키움봇 주간 스캔 시작 ({date_str}) ===")
    log.info(
        f"  market={args.market}, top_n={args.top}, "
        f"no_crash={args.no_crash}, dry_run={args.dry_run}"
    )

    # ── 1. 스캔 실행 ──────────────────────────────────
    with_crash = not args.no_crash
    try:
        text_result, _ = kium_bot.run(
            action="scan",
            top_n=args.top,
            market=args.market,
            with_crash_signals=with_crash,
        )
    except Exception as exc:
        log.exception(f"kium_bot.run 실패: {exc}")
        sys.exit(1)

    print(text_result)

    if args.dry_run:
        log.info("dry-run 모드 — 저장·푸시 스킵")
        return

    # ── 2. HTML용 상세 데이터 추출 ──────────────────────
    crash_signals: dict | None = None
    weight: dict | None = None
    results_for_html: list[dict] = []

    if with_crash:
        try:
            results_for_html = kium_bot.scan_universe(market=args.market, top_n=args.top)
            kospi_close = kium_bot.fetch_kospi_close()
            crash_signals = kium_bot.detect_crash_signals(
                kospi_close=kospi_close, top_results=results_for_html
            )
            vkospi_v = kium_bot.fetch_vkospi_latest()
            weight = kium_bot.compute_weight_recommendation(
                vkospi=vkospi_v, kospi_close=kospi_close
            )
        except Exception as exc:
            log.warning(f"HTML용 상세 데이터 추출 실패 — 텍스트 결과만 저장: {exc}")

    # ── 3. HTML 리포트 생성 ──────────────────────────────
    html_result = make_html_report(
        results=results_for_html,
        crash_signals=crash_signals,
        weight=weight,
        market=args.market,
    )

    # ── 4. 리포트 저장 ────────────────────────────────────
    txt_path, html_path = save_reports(text_result, html_result, date_str)
    log.info(f"리포트 경로: {html_path}")

    # ── 5. 텔레그램 푸시 ────────────────────────────────
    date_display = datetime.now().strftime("%Y-%m-%d")
    tg_text = f"🗓 <b>주간 모멘텀 스캔 ({date_display})</b>\n{text_result}"
    sent = push_telegram(tg_text)
    if sent == 0:
        log.warning("텔레그램 푸시 실패 또는 환경변수 미설정")

    # ── 6. Slack 푸시 (웹훅 있을 때만) ──────────────────
    push_slack(text_result)

    log.info(
        f"=== 키움봇 주간 스캔 완료 ({date_str}) "
        f"— 텔레그램 {sent}건 전송 ==="
    )


if __name__ == "__main__":
    main()
