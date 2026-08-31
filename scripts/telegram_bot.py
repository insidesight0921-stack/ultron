#!/usr/bin/env python3
"""
텔레그램 봇 — 폰에서 어디서든 RAG/메모/URL/파일 + 마스터 라우팅 + 멀티턴.

4단계 진입 — 라우팅 아키텍처:
  사용자 메시지 (텍스트, URL/파일/슬래시 명령 제외)
    → 직전 대화 history 가져옴
    → router.route(query, history)  [Gemma 4 26B MoE, JSON 출력]
    → tool 실행 (knowledge_bot OR respond_directly)
    → 응답 + history에 새 턴 추가

기능:
- 텍스트 → 라우터 → knowledge_bot(31B Dense) 또는 즉답
- 슬래시 명령:
    /start  /help  /ping  /status  /clear (대화 메모리 초기화)
    /note   /notes [N]   /search <키워드>
- URL 자동 인식 — http(s) 메시지 → trafilatura 본문 추출 → raw/inbox/
- 파일 업로드 — .md/.txt/.pdf + 코드(.py/.js/.ts/.go/.rs/.java/.sh/.sql/.yaml/.json 등) → raw/inbox/
- ALLOWED_TELEGRAM_USER_ID 화이트리스트 (필수)
- chat_id 단위 멀티턴 메모리 (in-memory, 봇 재시작 시 휘발)
- 4096자 초과 답변 자동 분할

환경변수 (.env):
  TELEGRAM_BOT_TOKEN          BotFather에서 받은 토큰
  ALLOWED_TELEGRAM_USER_ID    본인 user_id (콤마 구분으로 여러 명 가능)
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import time as dtime

try:
    from zoneinfo import ZoneInfo

    _KST = ZoneInfo("Asia/Seoul")
except Exception:
    _KST = None
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")

# 공용 모듈
sys.path.insert(0, str(PROJECT / "scripts"))
from storage_paths import PATHS  # noqa: E402
from ask import LLM_MODEL  # noqa: E402  (지식봇 내부에서도 사용)
from inbox import (
    save_to_inbox,
    extract_url_content,
    extract_pdf_text,
    VAULT,
)  # noqa: E402
from memory import ChatMemory  # noqa: E402
from router import route, MASTER_MODEL  # noqa: E402
from knowledge_bot import run as knowledge_run  # noqa: E402
from schedule_bot import run as schedule_run  # noqa: E402
from finance_bot import run as finance_run  # noqa: E402
from invest_bot import run as invest_run  # noqa: E402
from watchlist_bot import run as watchlist_run  # noqa: E402
from telegram_write_identity import (  # noqa: E402
    build_schedule_write_identity,
    build_watchlist_write_identity,
)
from paper_trade_identity import build_paper_trade_write_identity  # noqa: E402
from private_write_runtime import (  # noqa: E402
    PrivateWriteRuntimeError,
    load_private_write_runtime_bundle,
)
from private_write_cutover import PrivateWriteCutoverError  # noqa: E402
from private_write_readiness import PrivateWriteReadinessError  # noqa: E402
import telegram_notify  # noqa: E402  # v3.56 기동 실패 알림(봇과 독립)
from kium_bot import run as kium_run, scan_universe as kium_scan  # noqa: E402
from quant_bot import (
    run as quant_run,
    recommend_top_n as quant_recommend,
    snapshot as quant_snapshot,
)  # noqa: E402  # v3.22 콴텍봇
from ipo_bot import (
    fetch_ipo_schedule,
    run as ipo_run,
    scan_upcoming as ipo_scan,
    diagnose_scan as ipo_diagnose_scan,       # v3.56 침묵 실패 구분
    missing_factors as ipo_missing_factors,   # v3.56 무엇이 비었는지
)  # noqa: E402  # v3.26 IPO봇
import signal_bot  # noqa: E402  # v3.40 기술적 신호 봇
import idle_cash  # noqa: E402  # v3.57 유휴 슬롯 자본 파킹
from news_bot import run as news_run  # noqa: E402  # v3.41 뉴스봇
import agent_bot  # noqa: E402  # v3.42 범용 에이전트
import action_scheduler as _asch  # noqa: E402  # v3.43 봇작업 예약
import research_bot as _research  # noqa: E402  # v3.44 RAG미스 웹폴백
import system_info as _sysinfo  # noqa: E402  # v3.45 봇 자기/시스템 인식
import paper_analytics as _panalytics  # noqa: E402  # v3.46 paper 성과 리포트
import strategy_feedback as _sfeedback  # noqa: E402  # v3.46 성과→비중 제안(추천 전용)
import paper_db as _pdb  # noqa: E402  # v3.29 paper trading 승인
from inbox_bot import run as inbox_bot_run  # noqa: E402
from coding_bot import run as coding_run  # noqa: E402

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─── 설정 ────────────────────────────────────────────

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
RAW_ALLOWED = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()

if not TOKEN:
    sys.exit("❌ TELEGRAM_BOT_TOKEN 미설정. .env 확인.")
if not RAW_ALLOWED:
    sys.exit("❌ ALLOWED_TELEGRAM_USER_ID 미설정. 보안상 화이트리스트 필수.")

try:
    ALLOWED_IDS: set[int] = {
        int(x.strip()) for x in RAW_ALLOWED.split(",") if x.strip()
    }
except ValueError:
    sys.exit(f"❌ ALLOWED_TELEGRAM_USER_ID 파싱 실패 (숫자 콤마 구분): {RAW_ALLOWED!r}")

if not ALLOWED_IDS:
    sys.exit("❌ ALLOWED_TELEGRAM_USER_ID에 유효한 ID 없음.")

WIKI = VAULT / "wiki"
RAW_INBOX = VAULT / "raw" / "inbox"

TELEGRAM_MAX = 4096
URL_RE = re.compile(r"^https?://\S+", re.IGNORECASE)

MAX_FILE_BYTES = 20 * 1024 * 1024
# 텍스트 첨부 — 그대로 읽음
TEXT_FILE_EXTS = {".md", ".txt"}
# 코드 첨부 (B-3, v3.13) — UTF-8 텍스트로 읽음. 추후 coding_bot 입력으로도 활용 가능
CODE_FILE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".rb",
    ".java",
    ".kt",
    ".swift",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".xml",
    ".ini",
    ".cfg",
    ".dockerfile",
}
# PDF — pdfminer로 텍스트 추출
PDF_FILE_EXTS = {".pdf"}
ALLOWED_FILE_EXTS = TEXT_FILE_EXTS | CODE_FILE_EXTS | PDF_FILE_EXTS

# 라우터에 넘길 직전 N턴 (왕복 = user+assistant 2턴)
ROUTER_HISTORY_TURNS = 6
KNOWLEDGE_HISTORY_TURNS = 6

# 멀티턴 메모리 (chat_id 단위)
memory = ChatMemory(max_turns=12)

# 로깅
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.WARNING)
logging.getLogger("pdfminer").setLevel(logging.WARNING)
log = logging.getLogger("telegram_bot")

# main()에서만 설정한다. bundle 환경키가 없으면 계속 None이며 기존 직접 쓰기를 유지한다.
_PRIVATE_WRITE_CLIENT = None
_PRIVATE_WRITE_EXECUTOR = None
_PRIVATE_SCHEDULE_WRITE_EXECUTOR = None
_PRIVATE_PAPER_WRITE_CLIENT = None
_PRIVATE_PAPER_WRITE_EXECUTOR = None


def _list_runtime_paper_slots() -> list[dict]:
    if _PRIVATE_PAPER_WRITE_CLIENT is not None:
        return _PRIVATE_PAPER_WRITE_CLIENT.list_paper_slots()
    return _pdb.list_slots()


def _list_runtime_paper_positions(slot=None) -> list[dict]:
    if _PRIVATE_PAPER_WRITE_CLIENT is not None:
        return _PRIVATE_PAPER_WRITE_CLIENT.list_paper_positions(slot=slot)
    return _pdb.list_positions(slot=slot)


def _runtime_paper_slot_summary(slot_id: int) -> dict | None:
    if _PRIVATE_PAPER_WRITE_CLIENT is not None:
        return next(
            (
                slot
                for slot in _PRIVATE_PAPER_WRITE_CLIENT.list_paper_slots()
                if int(slot["id"]) == int(slot_id)
            ),
            None,
        )
    return _pdb.slot_summary(slot_id)


async def _execute_private_paper_write(
    *,
    caller: str,
    action: str,
    slot_id: int,
    actor_id: object,
    source_event_id: object,
    item_key: object,
    ticker: str,
    quantity: int,
    price: float,
    name: str | None = None,
    notes: str | None = None,
    user_approved: bool,
    policy_approved: bool,
):
    if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
        raise RuntimeError("Private Paper write executor is unavailable")
    identity = build_paper_trade_write_identity(
        caller=caller,
        operation=f"paper.{action}",
        slot_id=slot_id,
        actor_id=actor_id,
        source_event_id=source_event_id,
        item_key=item_key,
    )
    return await asyncio.to_thread(
        _PRIVATE_PAPER_WRITE_EXECUTOR.execute,
        caller=caller,
        action=action,
        slot_id=slot_id,
        identity=identity,
        user_approved=user_approved,
        policy_approved=policy_approved,
        ticker=ticker,
        name=name,
        quantity=quantity,
        price=price,
        notes=notes,
    )


class PrivateWriteLocked(RuntimeError):
    """Private write 경로를 쓸 수 없는 상태에서 원본 DB 직접 쓰기를 막는다."""


_PRIVATE_WRITE_LOCKED = False
_PRIVATE_WRITE_LOCK_REASON = ""

# 기동 게이트가 막는 것은 **쓰기**여야 한다 (2026-08-28 결정).
#
# 이전에는 번들 로딩이 실패하면 봇 프로세스 자체가 죽었다. 그런데 장 중 손절·익절
# 모니터가 이 프로세스 안에서 돈다. 데이터를 지키려는 가드가 **손절 감시를 꺼서
# 더 큰 위험을 만들고** 있었다. 이제 봇은 뜨고 쓰기만 잠근다.
#
# 다만 executor를 None으로 두는 것만으로는 부족하다. 폴백 경로가 `_pdb`로 **원본
# DB에 직접** 쓰기 때문에, 그대로 두면 번들이 끄겠다고 선언한 통로
# (`direct_db_fallback_disabled`)가 오히려 활짝 열린다. 그래서 잠금 상태에서는
# 직접 쓰기도 거부한다.
#
#   executor 있음            → 정상 경로
#   executor 없음 + 잠금 아님 → 직접 쓰기 (Private write 미도입 환경)
#   executor 없음 + 잠금      → **거부**


# 잠금이 막는 것은 **새 위험을 만드는 쓰기**다(2026-08-31 정정).
#
# 처음엔 매수·청산을 가리지 않고 전부 막았다. 그 결과 실제로 이런 로그가 남았다.
#
#   09:08:50 익절 기록 실패 010120: Private write 잠금 — 직접 쓰기 거부
#
# **청산까지 막으면 안 된다.** 이 프로젝트의 기존 원칙이 그렇다 — 매수는
# fail-closed(모르면 사지 않는다), 청산은 fail-open(못 팔면 손실이 커진다).
# 잠금의 목적은 "복구 가능한 백업 없이 **새 포지션을 만들지 않는 것**"이지
# 이미 가진 포지션을 정리하지 못하게 하는 것이 아니다. 게이트가 막으려던 위험보다
# 게이트가 만든 위험이 커지는 순간이다(같은 이유로 봇 기동 자체는 막지 않는다).
_EXIT_WRITERS = ("record_sell",)


def _direct_paper_write(fn, *args, **kwargs):
    """executor가 없을 때의 직접 쓰기. 잠금 상태에서 **매수만** 거부한다."""
    is_exit = getattr(fn, "__name__", "") in _EXIT_WRITERS
    if _PRIVATE_WRITE_LOCKED:
        if not is_exit:
            raise PrivateWriteLocked(
                f"Private write 잠금 — 신규 매수 거부 ({_PRIVATE_WRITE_LOCK_REASON})")
        log.warning("Private write 잠금 중이나 **청산은 기록한다** — "
                    "막으면 손실이 커진다 (사유: %s)", _PRIVATE_WRITE_LOCK_REASON)
    return fn(*args, **kwargs)


def _start_private_write_runtime() -> None:
    """번들을 싣는다. 게이트가 막으면 **쓰기만 잠그고 봇은 계속 뜬다.**

    잡는 예외는 게이트가 내는 것만이다. 넓게 잡으면 코딩 오류까지 "게이트 때문"으로
    보여서 원인을 알 수 없게 된다 — 이미 `.env` 로딩에서 한 번 겪었다.
    """
    global _PRIVATE_WRITE_LOCKED, _PRIVATE_WRITE_LOCK_REASON
    try:
        _configure_private_write_runtime(load_private_write_runtime_bundle())
    except (PrivateWriteRuntimeError, PrivateWriteCutoverError,
            PrivateWriteReadinessError) as exc:
        _configure_private_write_runtime(None)
        _PRIVATE_WRITE_LOCKED = True
        _PRIVATE_WRITE_LOCK_REASON = str(exc)
        log.error("Private write 잠금 — 봇은 기동하되 쓰기를 막습니다: %s", exc)
        try:
            telegram_notify.send(
                "🔒 Private write 잠금\n"
                f"사유: {exc}\n\n"
                "봇은 정상 기동했고 조회·알림·장중 손절 모니터는 그대로 돕니다.\n"
                "**청산(손절·익절)은 계속 기록됩니다.** 신규 매수만 막힙니다.\n"
                "복구: python3 scripts/private_data_security.py all → 서비스 재시작")
        except Exception:  # noqa: BLE001
            log.warning("잠금 알림 발송 실패", exc_info=True)
        return
    _PRIVATE_WRITE_LOCKED = False
    _PRIVATE_WRITE_LOCK_REASON = ""


def _configure_private_write_runtime(runtime_bundle) -> None:
    global _PRIVATE_WRITE_CLIENT, _PRIVATE_WRITE_EXECUTOR
    global _PRIVATE_SCHEDULE_WRITE_EXECUTOR
    global _PRIVATE_PAPER_WRITE_CLIENT, _PRIVATE_PAPER_WRITE_EXECUTOR
    _PRIVATE_WRITE_CLIENT = None
    _PRIVATE_WRITE_EXECUTOR = None
    _PRIVATE_SCHEDULE_WRITE_EXECUTOR = None
    _PRIVATE_PAPER_WRITE_CLIENT = None
    _PRIVATE_PAPER_WRITE_EXECUTOR = None
    if runtime_bundle is None:
        return
    _PRIVATE_WRITE_CLIENT = runtime_bundle.build_client()
    _PRIVATE_WRITE_EXECUTOR = runtime_bundle.build_executor(_PRIVATE_WRITE_CLIENT)
    if runtime_bundle.schedule_writes_enabled:
        _PRIVATE_SCHEDULE_WRITE_EXECUTOR = runtime_bundle.build_schedule_executor(
            _PRIVATE_WRITE_CLIENT
        )
    if runtime_bundle.paper_writes_enabled:
        _PRIVATE_PAPER_WRITE_CLIENT = runtime_bundle.build_paper_client()
        _PRIVATE_PAPER_WRITE_EXECUTOR = runtime_bundle.build_paper_executor(
            _PRIVATE_PAPER_WRITE_CLIENT
        )


# ─── 유틸 ────────────────────────────────────────────


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ALLOWED_IDS


def deny_log(update: Update, action: str) -> None:
    user = update.effective_user
    uid = user.id if user else "?"
    uname = user.username if user else "?"
    log.warning(f"⛔ 접근 거부 [{action}] user_id={uid} username={uname}")


def split_for_telegram(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


def format_sources(chunks: list[dict]) -> str:
    if not chunks:
        return ""
    lines = ["", "📚 참조한 노트"]
    for i, c in enumerate(chunks, 1):
        f = c["file"].replace("wiki/", "")
        sec = c.get("section", "")
        d = c.get("_distance", 0)
        lines.append(f"{i}. {f} :: {sec}  (거리 {d:.3f})")
    return "\n".join(lines)


def list_recent_wiki_notes(n: int = 10) -> list[Path]:
    if not WIKI.exists():
        return []
    files = [p for p in WIKI.rglob("*.md") if not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:n]


def search_wiki_titles(keyword: str, limit: int = 30) -> list[Path]:
    if not WIKI.exists() or not keyword:
        return []
    kw_nfc = unicodedata.normalize("NFC", keyword.lower())
    matches = []
    for p in WIKI.rglob("*.md"):
        if p.name.startswith("."):
            continue
        name_nfc = unicodedata.normalize("NFC", p.stem.lower())
        if kw_nfc in name_nfc:
            matches.append(p)
            if len(matches) >= limit:
                break
    return matches


# ─── 핸들러 ──────────────────────────────────────────

WELCOME = (
    "🤖 현준의 RAG 비서 (4단계 — 지식봇 + 일정봇 + 금융봇 + 투자봇)\n"
    "\n"
    "마스터 에이전트가 질문을 분석해서 적절한 도구로 위임합니다.\n"
    '직전 대화도 기억해 — "그거 더 자세히" 같은 후속 질문 가능.\n'
    "\n"
    "예시:\n"
    '  • "내 매매 청산 규칙은?" → 지식봇 (wiki RAG)\n'
    '  • "내일 오후 3시 콴텍봇 리뷰 잡아줘" → 일정봇 (등록 + 자동 알림)\n'
    '  • "VIX 지금 몇이야?" → 금융봇 (FRED)\n'
    '  • "지금 시장이 내 매매 원칙에 맞아?" → 금융봇 (지표 + Wiki 대조)\n'
    '  • "삼성전자 차트 봐줘" → 투자봇 (지표만, 빠름)\n'
    '  • "삼성전자 매수 조건 충족해?" → 투자봇 (원칙 대조 + 31B 평가)\n'
    '  • "...라고 메모해줘" / "기록해" → 메모봇 (raw/inbox/ 자동 저장)\n'
    '  • "이 함수 디버깅" / "파이썬 클래스 설계" → 코딩봇 (Qwen/Claude)\n'
    "\n"
    "🚀 fast 모드 / 🔍 accurate 모드 — 라우터가 입력 보고 자동 결정\n"
    "\n"
    "/help — 명령 목록\n"
    "/clear — 대화 메모리 초기화"
)

HELP = (
    "🧭 명령 목록\n"
    "\n"
    "📥 정보 입력\n"
    "/note <내용>      명시적 메모 저장 (라우팅 우회, 즉시)\n"
    '자연어 메모        "...라고 메모해줘" → 라우터가 inbox_bot으로 분기\n'
    "URL 그대로 전송   본문 추출 후 raw/inbox/ 저장\n"
    "파일 업로드        .md/.txt/.pdf + 코드(.py/.js/.ts 등) → raw/inbox/\n"
    "\n"
    "🔍 검색/조회\n"
    "/notes [N]        최근 정제된 wiki 노트 N개 (기본 10)\n"
    "/search <키워드>  wiki 제목 빠른 검색\n"
    "\n"
    "📅 일정\n"
    '자연어로 말해 — "다음주 화요일 오후 3시 콴텍봇 리뷰"\n'
    '반복: "매일 오전 7시 운동" / "매주 월수금 9시 리뷰"\n'
    '사전 알림: "내일 3시 회의 5분 전 알려줘"\n'
    '조회: "내 일정" / "다가오는 일정"\n'
    '삭제: "#3 삭제" / "3번 일정 지워줘"\n'
    "🔔 시각 도달 시 자동 푸시 (60초 주기 스캔, 12시간 grace)\n"
    "\n"
    "📊 경제 지표 / 시장 점검\n"
    '단일: "VIX 지금?", "환율 얼마야?", "기준금리"\n'
    '묶음: "경제 지표 보여줘" / "대시보드"\n'
    '원칙 대조: "지금 시장이 내 매매 원칙이랑 맞아?"\n'
    "\n"
    "📈 종목 분석 (단일 종목, 실주문 X)\n"
    '지표만: "삼성전자 차트" / "005930 분석"\n'
    '원칙 대조: "삼성전자 매수 조건 충족해?" / "내 원칙 기준 평가"\n'
    "\n"
    "💻 코딩 (Qwen2.5-Coder 로컬 + Claude API 하이브리드)\n"
    '설계: "파이썬 장바구니 클래스 설계"\n'
    '구현: "피보나치 N번째 함수 구현"\n'
    '디버깅: "이 에러 왜 나? TypeError ..."\n'
    '리뷰: "이 코드 리뷰해줘" + 파일 첨부\n'
    "\n"
    "🛠 시스템\n"
    "/start            시작 인사\n"
    "/help             이 도움말\n"
    "/ping             봇 살아있는지\n"
    "/status           인덱스/모델/일정 상태\n"
    "/clear            이 채팅의 대화 메모리 초기화\n"
    "/agent <명령>     도구를 조합해 다단계 수행(범용 에이전트)\n"
    '자연어 예약: "매일 8시 뉴스 보내", "자동작업 목록", "자동작업 2 삭제"\n'
    "\n"
    "그 외 모든 메시지는 마스터 라우터 → 적절한 도구로 처리\n"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/start")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/help")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    await update.message.reply_text(HELP)


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/ping")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    await update.message.reply_text("pong 🏓")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/status")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    try:
        import lancedb
        from ask import DB_PATH, TABLE_NAME

        db = lancedb.connect(str(DB_PATH))
        n = (
            db.open_table(TABLE_NAME).count_rows()
            if TABLE_NAME in db.table_names()
            else 0
        )
        inbox_count = len(list(RAW_INBOX.glob("*.md"))) if RAW_INBOX.exists() else 0
        chat_id = str(update.effective_chat.id)
        my_turns = len(memory.history(chat_id))
        all_chats = len(memory.stats())
        # 일정봇 통계
        try:
            from schedule_bot import (
                list_events as _list_events,
                due_for_notification as _due,
            )

            all_events = _list_events(
                upcoming_only=False, limit=500, include_completed=True
            )
            upcoming_n = len(_list_events(upcoming_only=True, limit=100))
            recurring_n = sum(1 for e in all_events if e.get("rrule_freq"))
            pre_enabled_n = sum(1 for e in all_events if e.get("pre_notify_minutes"))
            pending_notify = len(_due(horizon_seconds=NOTIFY_HORIZON_SEC))
        except Exception:
            upcoming_n = -1
            recurring_n = -1
            pre_enabled_n = -1
            pending_notify = -1
        msg = (
            f"✅ 가동 중\n"
            f"• 인덱스: {n} chunks\n"
            f"• 마스터: {MASTER_MODEL} (라우팅)\n"
            f"• 하위: {LLM_MODEL} (지식봇/학습봇/금융봇/투자봇) + Qwen·Claude(coding)\n"
            f"• inbox 대기: {inbox_count}개 파일\n"
            f"• 다가오는 일정: {upcoming_n if upcoming_n >= 0 else '?'}개\n"
            f"• 반복 일정: {recurring_n if recurring_n >= 0 else '?'}건\n"
            f"• 사전 알림 사용: {pre_enabled_n if pre_enabled_n >= 0 else '?'}건\n"
            f"• 알림 대기: {pending_notify if pending_notify >= 0 else '?'}건\n"
            f"• 이 채팅 메모리: {my_turns}턴\n"
            f"• 활성 채팅 수: {all_chats}"
        )
    except Exception as e:
        msg = f"⚠️ 상태 확인 실패: {e}"
    await update.message.reply_text(msg)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """대화 메모리 초기화."""
    if not is_authorized(update):
        deny_log(update, "/clear")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    chat_id = str(update.effective_chat.id)
    cleared = memory.clear(chat_id)
    if cleared:
        await update.message.reply_text(
            f"🧹 대화 메모리 초기화 완료 ({cleared}턴 삭제)"
        )
    else:
        await update.message.reply_text("이 채팅에 저장된 대화가 없습니다.")
    log.info(f"🧹 /clear chat_id={chat_id} cleared={cleared}")


def _agent_tool_safe(fn):
    """에이전트 도구 래퍼 — 예외를 문자열로 변환(루프 graceful)."""

    def _w(args):
        try:
            r = fn(args)
            return r if isinstance(r, str) else str(r)
        except Exception as e:
            return f"오류: {e}"

    return _w


def _setup_agent_tools() -> None:
    """agent_bot에 안전 도구 + 기존 봇을 등록(봇 시작 시 1회)."""
    agent_bot.clear_tools()
    agent_bot.register_builtin_tools()  # read_file / list_files (샌드박스, 읽기)
    agent_bot.register_inbox_tool()  # write_inbox (raw/inbox 한정 쓰기)
    agent_bot.register_tool(
        agent_bot.Tool(
            "news",
            "IT/AI 뉴스 헤드라인+요약",
            _agent_tool_safe(lambda a: news_run()[0] or "신규 기사 없음"),
        )
    )
    agent_bot.register_tool(
        agent_bot.Tool(
            "signal_scan",
            "핵심 자산배분 기술적 매매 신호(1시간봉)",
            _agent_tool_safe(
                lambda a: signal_bot.run()[0] or "현재 actionable 신호 없음"
            ),
        )
    )
    agent_bot.register_tool(
        agent_bot.Tool(
            "ipo_scan",
            "향후 공모주 일정·매력지수 스캔",
            _agent_tool_safe(
                lambda a: ipo_run(action="scan", days_ahead=int(a.get("days", 30)))[0]
            ),
            args_hint='{"days": 30}',
        )
    )
    agent_bot.register_tool(
        agent_bot.Tool(
            "quant_phase",
            "거시 경기국면 진단(콴텍봇)",
            _agent_tool_safe(lambda a: quant_run(action="phase")[0]),
        )
    )
    agent_bot.register_tool(
        agent_bot.Tool(
            "rag_search",
            "내 위키(투자 원칙·노트)에서 검색·답변",
            _agent_tool_safe(
                lambda a: knowledge_run(a.get("query", ""), None, None, "fast")[0]
            ),
            args_hint='{"query": "리스크 관리 원칙"}',
        )
    )
    log.info(f"🤖 에이전트 도구 {len(agent_bot.list_tools())}개 등록")


async def cmd_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/agent <명령> — 도구를 조합해 다단계 수행(로컬 Gemma)."""
    if not is_authorized(update):
        deny_log(update, "/agent")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    task = " ".join(ctx.args).strip() if ctx.args else ""
    if not task:
        await update.message.reply_text(
            "사용법: /agent <명령>\n예: /agent 오늘 신호랑 IT 뉴스 같이 정리해줘"
        )
        return
    notice = await update.message.reply_text(
        "🤖 에이전트 작업 중... (다단계, 최대 1~2분)"
    )
    try:
        ans = await asyncio.to_thread(agent_bot.run, task)
    except Exception as e:
        log.exception("agent_bot 실패")
        await notice.edit_text(f"❌ 에이전트 오류: {e}")
        return
    await notice.edit_text((ans or "(빈 응답)")[:4000])


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/note")
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    full = update.message.text or ""
    parts = full.split(maxsplit=1)
    content = parts[1].strip() if len(parts) > 1 else ""

    if not content:
        await update.message.reply_text(
            "사용법: /note <내용>\n\n"
            "예) /note 삼성전자 26.4Q 영업이익 컨센 상회. 다음 분기 가이던스 주목.\n"
            "여러 줄도 OK (Shift+Enter로 줄바꿈)."
        )
        return

    try:
        target = await asyncio.to_thread(
            save_to_inbox, content, "메모", "telegram /note"
        )
    except Exception as e:
        log.exception("inbox 저장 실패")
        await update.message.reply_text(f"❌ 저장 실패: {e}")
        return

    await update.message.reply_text(
        f"✅ 메모 저장됨\n"
        f"📁 {target.relative_to(VAULT)}\n"
        f"📏 {len(content)}자\n"
        f"\n"
        f"5분 idle 또는 30분 batch에 자동으로 정제 → wiki/에 인덱싱됩니다."
    )
    log.info(f"📝 /note 저장 user_id={update.effective_user.id} len={len(content)}")


async def cmd_notes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/notes")
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    args = ctx.args
    n = 10
    if args:
        try:
            n = max(1, min(50, int(args[0])))
        except ValueError:
            pass

    files = list_recent_wiki_notes(n)
    if not files:
        await update.message.reply_text("wiki/ 에 노트 없음.")
        return

    now = time.time()
    lines = [f"📚 최근 wiki 노트 {len(files)}개\n"]
    for p in files:
        rel = p.relative_to(WIKI)
        age_sec = now - p.stat().st_mtime
        if age_sec < 3600:
            age = f"{int(age_sec / 60)}분 전"
        elif age_sec < 86400:
            age = f"{int(age_sec / 3600)}시간 전"
        else:
            age = f"{int(age_sec / 86400)}일 전"
        lines.append(f"• {rel}  ({age})")

    text = "\n".join(lines)
    for part in split_for_telegram(text):
        await update.message.reply_text(part)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "/search")
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    args = ctx.args
    if not args:
        await update.message.reply_text(
            "사용법: /search <키워드>\n\n"
            "예) /search 모멘텀\n"
            "    /search IPO\n"
            "\n"
            "(제목만 검색합니다. 본문 검색은 그냥 질문 입력 → RAG 사용)"
        )
        return

    keyword = " ".join(args).strip()
    matches = await asyncio.to_thread(search_wiki_titles, keyword, 30)

    if not matches:
        await update.message.reply_text(f"'{keyword}' 일치 노트 없음.")
        return

    lines = [f"🔍 '{keyword}' 일치 노트 {len(matches)}개\n"]
    for p in matches:
        rel = p.relative_to(WIKI)
        lines.append(f"• {rel}")

    text = "\n".join(lines)
    for part in split_for_telegram(text):
        await update.message.reply_text(part)


# ─── URL 처리 ────────────────────────────────────────


async def handle_url(update: Update, url: str) -> None:
    notice = await update.message.reply_text("🌐 본문 추출 중...")
    try:
        result = await asyncio.to_thread(extract_url_content, url)
    except Exception as e:
        log.exception("URL 추출 예외")
        await notice.edit_text(f"❌ 추출 실패: {e}")
        return

    if not result:
        await notice.edit_text(
            "⚠️ 본문 추출 실패 — JS 렌더링이거나 차단됐을 수 있어.\n"
            "직접 텍스트로 복사해서 /note 로 보내봐."
        )
        return

    title, body = result
    full = f"# {title}\n\nURL: {url}\n\n---\n\n{body}"

    try:
        target = await asyncio.to_thread(
            save_to_inbox, full, title, f"telegram URL: {url}"
        )
    except Exception as e:
        log.exception("inbox 저장 실패")
        await notice.edit_text(f"❌ 저장 실패: {e}")
        return

    await notice.edit_text(
        f"✅ URL 저장됨\n"
        f"📰 {title}\n"
        f"📁 {target.relative_to(VAULT)}\n"
        f"📏 본문 {len(body)}자\n"
        f"\n"
        f"자동 정제되어 wiki/에 인덱싱됩니다."
    )
    log.info(
        f"🌐 URL 저장 user_id={update.effective_user.id} url={url[:80]} len={len(body)}"
    )


# ─── 파일 업로드 ─────────────────────────────────────


def _ext_to_lang(ext: str) -> str:
    """확장자를 코드블록 언어 힌트로. 알 수 없으면 빈 문자열."""
    ext = ext.lower().lstrip(".")
    return {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "tsx",
        "jsx": "jsx",
        "mjs": "javascript",
        "cjs": "javascript",
        "go": "go",
        "rs": "rust",
        "rb": "ruby",
        "java": "java",
        "kt": "kotlin",
        "swift": "swift",
        "c": "c",
        "cc": "cpp",
        "cpp": "cpp",
        "cxx": "cpp",
        "h": "c",
        "hpp": "cpp",
        "sh": "bash",
        "bash": "bash",
        "zsh": "bash",
        "sql": "sql",
        "html": "html",
        "css": "css",
        "scss": "scss",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "json": "json",
        "xml": "xml",
        "ini": "ini",
        "cfg": "ini",
        "dockerfile": "dockerfile",
    }.get(ext, "")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "document")
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or "unknown"
    fsize = doc.file_size or 0
    ext = Path(fname).suffix.lower()

    if ext not in ALLOWED_FILE_EXTS:
        await update.message.reply_text(
            f"⚠️ 지원 안 함: {ext or '확장자 없음'}\n"
            f"허용: {', '.join(sorted(ALLOWED_FILE_EXTS))}"
        )
        return

    if fsize > MAX_FILE_BYTES:
        await update.message.reply_text(
            f"⚠️ 파일 너무 큼 ({fsize / 1024 / 1024:.1f}MB).\n"
            f"텔레그램 봇 API 한계 = {MAX_FILE_BYTES / 1024 / 1024:.0f}MB"
        )
        return

    notice = await update.message.reply_text(
        f"📥 다운로드 중... ({fname}, {fsize / 1024:.1f}KB)"
    )

    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await tg_file.download_to_drive(custom_path=str(tmp_path))
    except Exception as e:
        log.exception("파일 다운로드 실패")
        await notice.edit_text(f"❌ 다운로드 실패: {e}")
        return

    try:
        if ext in TEXT_FILE_EXTS or ext in CODE_FILE_EXTS:
            content = await asyncio.to_thread(
                lambda: tmp_path.read_text(encoding="utf-8", errors="replace")
            )
            kind = "코드" if ext in CODE_FILE_EXTS else "텍스트"
            extract_msg = f"{kind} {len(content)}자"
        elif ext == ".pdf":
            await notice.edit_text("📄 PDF 텍스트 추출 중...")
            content = await asyncio.to_thread(extract_pdf_text, tmp_path)
            if content is None:
                await notice.edit_text(
                    "⚠️ PDF 텍스트 추출 실패.\n"
                    "스캔된 PDF(이미지)는 OCR이 필요해 — 6단계에서 추가 예정."
                )
                tmp_path.unlink(missing_ok=True)
                return
            extract_msg = f"본문 {len(content)}자"
        else:
            await notice.edit_text(f"⚠️ 처리 안 됨: {ext}")
            tmp_path.unlink(missing_ok=True)
            return
    finally:
        tmp_path.unlink(missing_ok=True)

    if not content.strip():
        await notice.edit_text("⚠️ 빈 파일 — 저장 안 함.")
        return

    prefix = Path(fname).stem
    try:
        # 코드 파일은 코드블록으로 감싸서 refine_raw가 본문 그대로 보존하게
        if ext in CODE_FILE_EXTS:
            lang = _ext_to_lang(ext)
            body_block = f"```{lang}\n{content.rstrip()}\n```"
            full = f"# {prefix}\n\n원본 파일: {fname} (코드)\n\n---\n\n{body_block}"
        else:
            full = f"# {prefix}\n\n원본 파일: {fname}\n\n---\n\n{content.strip()}"
        target = await asyncio.to_thread(
            save_to_inbox, full, prefix, f"telegram file: {fname}"
        )
    except Exception as e:
        log.exception("inbox 저장 실패")
        await notice.edit_text(f"❌ 저장 실패: {e}")
        return

    await notice.edit_text(
        f"✅ 파일 저장됨\n"
        f"📎 {fname}\n"
        f"📁 {target.relative_to(VAULT)}\n"
        f"📏 {extract_msg}\n"
        f"\n"
        f"자동 정제되어 wiki/에 인덱싱됩니다."
    )
    log.info(
        f"📎 파일 저장 user_id={update.effective_user.id} name={fname} len={len(content)}"
    )


# ─── 일반 텍스트 → 라우터 ────────────────────────────


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        deny_log(update, "text")
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()
    if not text:
        return

    # URL 자동 인식 — 라우팅 전에 가로챔
    if URL_RE.match(text):
        url = text.split()[0]
        await handle_url(update, url)
        return

    log.info(f"💬 user_id={user.id} chat_id={chat_id} query={text[:80]!r}")

    # v3.42 — 복합(다단계) 명령은 /agent 없이 자동으로 에이전트에 위임
    if agent_bot.is_compound_command(text):
        log.info("🤖 복합 명령 감지 → 에이전트 자동 위임")
        notice = await update.message.reply_text("🤖 복합 명령 — 에이전트가 처리 중...")
        try:
            ans = await asyncio.to_thread(agent_bot.run, text)
            memory.add(chat_id, "user", text)
            memory.add(chat_id, "assistant", ans)
            await notice.edit_text((ans or "(빈 응답)")[:4000])
        except Exception as e:
            log.exception("자동 에이전트 위임 실패")
            await notice.edit_text(f"❌ 에이전트 오류: {e}")
        return

    notice = await update.message.reply_text("🧭 라우팅 중...")

    # 1. 라우터 호출 (직전 N턴 history 포함)
    history = memory.history(chat_id, n=ROUTER_HISTORY_TURNS)
    try:
        action = await asyncio.to_thread(route, text, history)
    except Exception as e:
        log.exception("라우터 호출 실패")
        await notice.edit_text(f"❌ 라우팅 실패: {e}")
        return

    tool = action["tool"]
    args = action["args"]
    mode = action.get("mode", "accurate")
    mode_emoji = "🚀" if mode == "fast" else "🔍"

    # 2. 도구별 처리
    answer: str
    chunks: list[dict] = []

    if tool == "respond_directly":
        answer = args.get("answer", "").strip()
        if not answer:
            answer = "(빈 응답)"

    elif tool == "knowledge_bot":
        rewritten = args.get("query", text).strip() or text
        await notice.edit_text(
            f"{mode_emoji} wiki 검색 + 답변 생성 중... (q: {rewritten[:60]})"
        )
        try:
            kb_history = memory.history(chat_id, n=KNOWLEDGE_HISTORY_TURNS)
            answer, chunks = await asyncio.to_thread(
                knowledge_run, rewritten, kb_history, None, mode
            )
            # v3.44 — 위키에 충분한 근거가 없으면 웹 검색→정리→wiki 저장→답변
            # v3.45 — 단, 자기참조/개인 질문이면 웹으로 새지 않음(위키 답변 유지)
            if _research.rag_is_weak(chunks) and not _research.is_self_referential(
                rewritten
            ):
                await notice.edit_text(
                    f"{mode_emoji}🔎 위키에 없어 웹에서 검색·정리 중..."
                )
                answer = await asyncio.to_thread(
                    _research.research,
                    rewritten,
                    lambda prefix, content: save_to_inbox(
                        content, prefix=prefix, source="web_research"
                    ),
                )
                chunks = []
        except Exception as e:
            log.exception("knowledge_bot 실패")
            await notice.edit_text(f"❌ 지식봇 오류: {e}")
            return

    elif tool == "schedule_bot":
        sched_action = args.get("action", "")
        await notice.edit_text(f"{mode_emoji}📅 일정봇 처리 중... ({sched_action})")
        try:
            write_identity = None
            if sched_action in {"add", "delete", "complete"}:
                write_identity = build_schedule_write_identity(
                    update_id=update.update_id,
                    chat_id=update.effective_chat.id,
                    user_id=update.effective_user.id,
                    message_id=update.message.message_id,
                    action=sched_action,
                )
            schedule_args = {
                **args,
                "chat_id": chat_id,
                "write_identity": write_identity,
            }
            if (
                sched_action in {"add", "delete", "complete"}
                and _PRIVATE_SCHEDULE_WRITE_EXECUTOR is not None
            ):
                schedule_args.update(
                    write_executor=_PRIVATE_SCHEDULE_WRITE_EXECUTOR,
                    write_user_approved=True,
                )
            answer, chunks = await asyncio.to_thread(schedule_run, **schedule_args)
        except Exception as e:
            log.exception("schedule_bot 실패")
            await notice.edit_text(f"❌ 일정봇 오류: {e}")
            return

    elif tool == "finance_bot":
        fin_action = args.get("action", "")
        if fin_action == "compare_with_principles":
            await notice.edit_text(f"{mode_emoji} 지표 수집 + Wiki 원칙 대조 중...")
        else:
            await notice.edit_text(f"{mode_emoji} 지표 조회 중... ({fin_action})")
        try:
            answer, chunks = await asyncio.to_thread(finance_run, **args)
        except Exception as e:
            log.exception("finance_bot 실패")
            await notice.edit_text(f"❌ 금융봇 오류: {e}")
            return

    elif tool == "invest_bot":
        inv_action = args.get("action", "")
        ticker = args.get("ticker_or_name", "")
        if inv_action == "compare_with_rules":
            await notice.edit_text(
                f"{mode_emoji} {ticker} 차트 + 매매 원칙 대조 중..."
                if mode == "accurate"
                else f"{mode_emoji} {ticker} 빠른 지표 조회 중 (LLM 무호출)..."
            )
        else:
            await notice.edit_text(f"{mode_emoji} {ticker} 차트 분석 중...")
        try:
            answer, chunks = await asyncio.to_thread(invest_run, mode=mode, **args)
        except Exception as e:
            log.exception("invest_bot 실패")
            await notice.edit_text(f"❌ 투자봇 오류: {e}")
            return

    elif tool == "watchlist_bot":
        wl_action = args.get("action", "")
        await notice.edit_text(f"{mode_emoji}⭐ 관심종목 처리 중... ({wl_action})")
        try:
            write_identity = None
            if wl_action in {"add", "remove"}:
                write_identity = build_watchlist_write_identity(
                    update_id=update.update_id,
                    chat_id=update.effective_chat.id,
                    user_id=update.effective_user.id,
                    message_id=update.message.message_id,
                    action=wl_action,
                )
            watchlist_args = {**args, "write_identity": write_identity}
            if _PRIVATE_WRITE_CLIENT is not None:
                watchlist_args["private_client"] = _PRIVATE_WRITE_CLIENT
            if wl_action in {"add", "remove"} and _PRIVATE_WRITE_EXECUTOR is not None:
                watchlist_args.update(
                    write_executor=_PRIVATE_WRITE_EXECUTOR,
                    # add/remove를 요청한 동일한 사용자 메시지가 명시 승인 신호다.
                    write_user_approved=True,
                )
            answer, chunks = await asyncio.to_thread(watchlist_run, **watchlist_args)
        except Exception as e:
            log.exception("watchlist_bot 실패")
            await notice.edit_text(f"❌ 관심종목 처리 오류: {e}")
            return

    elif tool == "kium_bot":
        market = args.get("market", "KOSPI200")
        top_n = args.get("top_n", 10)
        await notice.edit_text(
            f"{mode_emoji}📊 키움봇 모멘텀 스캔 중... ({market}, Top {top_n})"
        )
        try:
            answer, chunks = await asyncio.wait_for(
                asyncio.to_thread(kium_run, **args),
                timeout=360,  # 첫 호출 OHLCV 직렬 fetch 최대 6분
            )
        except asyncio.TimeoutError:
            log.warning("kium_bot 타임아웃 (360s)")
            await notice.edit_text(
                "키움봇 타임아웃 (6분). KRX 서버 응답이 느립니다.\n"
                "캐시 없는 첫 실행은 최대 6분 걸립니다. 잠시 후 재시도해 주세요."
            )
            return
        except Exception as e:
            log.exception("kium_bot 실패")
            await notice.edit_text(f"❌ 키움봇 오류: {e}")
            return

    elif tool == "quant_bot":
        months = args.get("months", 24)
        action_q = args.get("action", "phase")
        timeout_q = (
            360 if action_q == "recommend" else 60
        )  # recommend는 OHLCV fetch로 최대 6분
        await notice.edit_text(
            f"{mode_emoji}🌐 콴텍봇 거시 국면 분석 중... ({months}M)"
        )
        try:
            answer, chunks = await asyncio.wait_for(
                asyncio.to_thread(quant_run, **args),
                timeout=timeout_q,
            )
        except asyncio.TimeoutError:
            log.warning(f"quant_bot 타임아웃 ({timeout_q}s)")
            await notice.edit_text(
                f"콴텍봇 타임아웃 ({timeout_q}초). KRX 서버 응답이 느립니다.\n"
                "캐시 없는 첫 실행은 최대 6분 걸립니다. 잠시 후 재시도해 주세요."
            )
            return
        except Exception as e:
            log.exception("quant_bot 실패")
            await notice.edit_text(f"❌ 콴텍봇 오류: {e}")
            return

    elif tool == "ipo_bot":
        ipo_action = args.get("action", "scan")
        if ipo_action == "analyze":
            corp_name = args.get("corp_name", "")
            await notice.edit_text(
                f"{mode_emoji}📋 [{corp_name}] IPO 매력도 분석 중..."
            )
        else:
            await notice.edit_text(f"{mode_emoji}📋 IPO봇 공모주 스캔 중...")
        try:
            answer, chunks = await asyncio.to_thread(ipo_run, **args)
        except Exception as e:
            log.exception("ipo_bot 실패")
            await notice.edit_text(f"❌ IPO봇 오류: {e}")
            return

    elif tool == "news_bot":
        await notice.edit_text(f"{mode_emoji}📰 IT/AI 뉴스 수집·요약 중...")
        try:
            answer, chunks = await asyncio.to_thread(news_run, **args)
            if not answer:
                answer = "신규 기사가 없습니다 (이미 보낸 기사 제외)."
        except Exception as e:
            log.exception("news_bot 실패")
            await notice.edit_text(f"❌ 뉴스봇 오류: {e}")
            return

    elif tool == "system_info":
        await notice.edit_text(f"{mode_emoji}🛰 시스템 상태 확인 중...")
        try:
            answer = _sysinfo.answer(args.get("topic"))
        except Exception as e:
            log.exception("system_info 실패")
            answer = f"❌ 시스템 정보 조회 오류: {e}"
        chunks = []

    elif tool == "action_schedule":
        try:
            answer = _handle_action_schedule(args)
        except Exception as e:
            log.exception("action_schedule 실패")
            answer = f"❌ 예약 처리 오류: {e}"
        chunks = []

    elif tool == "inbox_bot":
        await notice.edit_text(f"{mode_emoji}📝 메모 저장 중...")
        try:
            answer, chunks = await asyncio.to_thread(
                inbox_bot_run, **{**args, "chat_id": chat_id, "mode": mode}
            )
        except Exception as e:
            log.exception("inbox_bot 실패")
            await notice.edit_text(f"❌ 메모 저장 실패: {e}")
            return

    elif tool == "coding_bot":
        cd_action = args.get("action", "")
        model_hint = "Claude API" if mode == "accurate" else "Qwen 로컬"
        await notice.edit_text(
            f"{mode_emoji}💻 [{cd_action}] {model_hint}로 처리 중..."
        )
        try:
            answer, chunks = await asyncio.to_thread(coding_run, mode=mode, **args)
        except Exception as e:
            log.exception("coding_bot 실패")
            await notice.edit_text(f"❌ 코딩봇 오류: {e}")
            return

    else:
        # router에서 막아두긴 했지만 안전 네트
        log.error(f"미지원 tool: {tool}")
        await notice.edit_text(f"❌ 미지원 도구: {tool}")
        return

    if not answer:
        await notice.edit_text("(빈 응답이 돌아왔습니다.)")
        return

    # 3. 메모리 갱신 (사용자 원본 + 봇 답변)
    memory.add(chat_id, "user", text)
    memory.add(chat_id, "assistant", answer)

    # 4. 답변 + 출처 합쳐 분할 전송
    final = answer + format_sources(chunks)
    parts = split_for_telegram(final)
    try:
        await notice.edit_text(parts[0], disable_web_page_preview=True)
    except Exception:
        await notice.edit_text(parts[0], disable_web_page_preview=True)
    for part in parts[1:]:
        await update.message.reply_text(part, disable_web_page_preview=True)

    log.info(f"✅ 응답 완료 chat_id={chat_id} tool={tool} mode={mode} len={len(final)}")


async def handle_other(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """사진/음성/스티커 — Document는 별도 핸들러가 처리."""
    if not is_authorized(update):
        deny_log(update, "non-text")
        await update.message.reply_text("접근 권한이 없습니다.")
        return
    await update.message.reply_text(
        "📎 사진/음성/스티커는 처리하지 않습니다.\n"
        ".md/.txt/.pdf 또는 코드 파일(.py/.js/.ts/.go 등) 업로드는 가능합니다."
    )


# ─── 일정 알림 스케줄러 (v3.6) ───────────────────────


NOTIFY_INTERVAL_SEC = 60  # 60초마다 schedule.db 스캔
NOTIFY_HORIZON_SEC = 70  # 발화 임박 윈도우 (interval + 약간)

# v3.23 — 콴텍봇 월간 리밸런싱 자동 푸시 (매월 첫 영업일 09:30 KST)
REBALANCE_HOUR = 9
REBALANCE_MINUTE = 30
REBALANCE_FLAG_DIR = PATHS.private_state_dir
REBALANCE_FLAG_FILE = REBALANCE_FLAG_DIR / "quant_rebalance_last.json"
# 월간 리밸런싱 승인 대기 (user_id → StockRecommendation list) v3.29
# v3.59 — **디스크에도 저장한다.** 메모리에만 두면 재시작으로 사라지고,
# 사용자가 버튼을 눌러도 "저장된 추천 종목이 없습니다"가 뜬다. 2026-07이 그렇게
# 통째로 넘어갔다(푸시 07-05 → 다음 재시작 07-08 → 실행 0건).
_PENDING_REBALANCE: dict[int, list] = {}
_PENDING_REBALANCE_MONTH: dict[int, str] = {}  # user_id → month_key
REBALANCE_PENDING_FILE = REBALANCE_FLAG_DIR / "quant_rebalance_pending.json"
# 키움봇 주간 스캔 승인 대기 (v3.29)
KIUM_FLAG_FILE = REBALANCE_FLAG_DIR / "kium_weekly_last.json"
_PENDING_KIUM: dict[int, list] = {}  # user_id → list[dict]
_PENDING_KIUM_WEEK: dict[int, str] = {}  # user_id → week_key
KIUM_SCAN_HOUR = 9
KIUM_SCAN_MINUTE = 5  # 09:05 (launchd 09:00 기동 후)
KIUM_CHECK_INTERVAL_SEC = 6 * 60 * 60  # 6h 주기 검사 (하루 4번)
# v3.30 — IPO봇 주간 스캔 (매주 월요일 09:10 조회, A등급 이상만 알림)
IPO_FLAG_FILE = REBALANCE_FLAG_DIR / "ipo_weekly_last.json"
_PENDING_IPO: dict[int, list] = {}  # user_id → list[dict]
IPO_SCAN_HOUR = 9
IPO_SCAN_MINUTE = 10
IPO_CHECK_INTERVAL_SEC = 6 * 60 * 60  # 6h 주기
IPO_MIN_GRADE = {"A++", "A+", "A"}  # 이 등급 이상만 paper 알림

# 매일 09:30에 한 번 실행 — 첫 영업일이면 푸시. 멱등 플래그로 같은 달 중복 차단.
REBALANCE_CHECK_INTERVAL_SEC = 24 * 60 * 60  # 24h

# v3.31 — 장 중 실시간 손절 모니터링
# v3.44 — 실행 지연 수정: 30분→5분 폴링, 네이버 실시간 시세(pykrx 폴백),
#         판정은 exit_rules.should_exit(급락 하드스톱·트레일링 포함)로 위임.
import exit_rules  # noqa: E402  # 순수 판정 모듈(hermetic 테스트는 test_exit_rules.py)

INTRADAY_MONITOR_INTERVAL_SEC = int(os.getenv("INTRADAY_MONITOR_INTERVAL_SEC", 5 * 60))
INTRADAY_STOP_LOSS_PCT = exit_rules.STOP_PCT  # -7% 손절선 (EXIT_STOP_PCT로 조정)
INTRADAY_TAKE_PROFIT_PCT = exit_rules.TAKE_PCT  # +20% 익절선 (EXIT_TAKE_PCT로 조정)
_INTRADAY_PEAKS_PATH = PATHS.private_state_file("intraday_peaks.json")

# v3.40 — 기술적 신호 봇 (1시간봉, 장중)
SIGNAL_CHECK_INTERVAL_SEC = 60 * 60  # 1시간 간격
SIGNAL_OPEN_HOUR, SIGNAL_OPEN_MIN = 9, 0
SIGNAL_CLOSE_HOUR, SIGNAL_CLOSE_MIN = 15, 30
_SIGNAL_DEDUP_PATH = PATHS.private_state_file("signal_last.json")

# v3.41 — 뉴스 다이제스트 (매일 정시)
NEWS_CHECK_INTERVAL_SEC = 10 * 60  # 검사 주기(발송 아님). 실제 발송은 하루 1회(멱등)
NEWS_DIGEST_HOUR, NEWS_DIGEST_MIN = 8, 0
NEWS_DIGEST_UNTIL = "2026-06-11"  # 이 날짜까지만 발송(포함). None이면 무기한
_NEWS_DIGEST_FLAG = PATHS.private_state_file("news_digest_last.json")


# ─── pykrx 현재가 헬퍼 (v3.30) ────────────────────────────────────────────────


def _get_pykrx_price(ticker: str) -> float | None:
    """pykrx OHLCV 최신 종가 반환. 실패 시 None."""
    try:
        from pykrx import stock as _stk
        from datetime import datetime as _dt, timedelta as _td

        today = _dt.now().strftime("%Y%m%d")
        start = (_dt.now() - _td(days=5)).strftime("%Y%m%d")
        df = _stk.get_market_ohlcv(start, today, ticker)
        if df is not None and not df.empty:
            col = "종가" if "종가" in df.columns else "Close"
            if col in df.columns:
                return float(df[col].iloc[-1])
    except Exception as e:
        log.debug(f"pykrx price fetch 실패 ({ticker}): {e}")
    return None


# ─── 네이버 실시간 현재가 (v3.44) ─────────────────────────────────────────────

_NAVER_POLL_URLS = (
    "https://polling.finance.naver.com/api/realtime/domestic/stock/{ticker}",
    "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{ticker}",
)


def _get_naver_price(ticker: str) -> float | None:
    """네이버 금융 polling API 현재가(장중 실시간). 실패 시 None."""
    import json as _json
    from urllib.request import Request, urlopen

    for url in _NAVER_POLL_URLS:
        try:
            req = Request(
                url.format(ticker=ticker), headers={"User-Agent": "Mozilla/5.0"}
            )
            with urlopen(req, timeout=3) as r:
                price = exit_rules.parse_naver_price(
                    _json.loads(r.read().decode("utf-8"))
                )
            if price:
                return price
        except Exception as e:
            log.debug(f"naver price fetch 실패 ({ticker}, {url}): {e}")
    return None


def _get_realtime_price(ticker: str) -> float | None:
    """장중 현재가 — 네이버 실시간 우선, 실패 시 pykrx 최신 종가 폴백.

    **청산 판정 전용.** 가격을 아예 모르면 손절 자체를 못 하므로 폴백이 있는 편이 낫고,
    폴백이 연속되면 `exit_rules.naver_health`가 경고한다.
    신규 매수에는 쓰지 않는다 — `_get_execution_price`를 쓸 것.
    """
    return _get_naver_price(ticker) or _get_pykrx_price(ticker)


def _get_execution_price(ticker: str) -> float | None:
    """**신규 매수 체결가 전용 — 폴백 금지.** 네이버 실시간만 쓴다.

    `_get_pykrx_price`는 이름과 달리 '최신 일봉 종가'라, 장중에 부르면 전 거래일 종가가
    돌아온다. 그 값으로 매수를 기록하면 2026-06-08 사고(4거래일 전 종가로 8종목 진입 →
    22분 만에 전량 손절, −678만원)와 같은 일이 폴백 경로로 되살아난다.

    청산은 가격을 모르면 판정 자체를 못 하니 폴백이 낫지만, 매수는 **안 사면 그만**이다.
    못 얻으면 `execution_price`가 대기 큐로 넘긴다.
    """
    return _get_naver_price(ticker)


def _now_kst():
    """장중 게이트용 현재 시각 — 서버 타임존과 무관하게 KST(가능하면 aware)."""
    from datetime import datetime as _dt

    return _dt.now(_KST) if _KST else _dt.now()


def _load_intraday_state() -> dict:
    """{"peaks": {포지션키: 피크수익률}, "px": {티커: 직전 폴링가}}. 구형(피크만) 마이그레이션."""
    import json as _json

    try:
        d = _json.loads(_INTRADAY_PEAKS_PATH.read_text(encoding="utf-8"))
        if "peaks" in d or "px" in d:
            return {
                "peaks": d.get("peaks") or {},
                "px": d.get("px") or {},
                "naver_streak": int(d.get("naver_streak") or 0),
            }
        return {"peaks": d, "px": {}, "naver_streak": 0}  # v3.44 초기 형식
    except Exception:
        return {"peaks": {}, "px": {}, "naver_streak": 0}


def _save_intraday_state(state: dict) -> None:
    import json as _json

    try:
        _INTRADAY_PEAKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _INTRADAY_PEAKS_PATH.write_text(_json.dumps(state), encoding="utf-8")
    except Exception as e:
        log.warning(f"장중 모니터 상태 저장 실패: {e}")


def _slot_hard_stop_reason(slot_name: str) -> Optional[str]:
    """슬롯 일일 손실 한도로 신규 매수가 막혀 있으면 사유, 아니면 None.

    모듈·상태 파일이 없으면 None(허용). 한도를 '판정하지 못한 것'과 '한도에 걸린 것'은
    다르며, 전자로 매수를 막으면 원인 모를 차단이 된다.
    """
    try:
        import slot_hard_stop
        return slot_hard_stop.guard_buy(str(slot_name))
    except Exception as exc:  # noqa: BLE001
        log.warning("일일 한도 확인 실패(매수 허용): %s", exc)
        return None



# ─── 장 중 실시간 손절·익절 모니터 (v3.31, 판정 v3.44=exit_rules) ─────────────


async def _drain_pending_orders(ctx: ContextTypes.DEFAULT_TYPE, now) -> list[str]:
    """v3.50: 장외에 쌓인 대기 주문을 개장 후 실제 가격으로 재평가해 체결한다.

    장외 기록가(대개 당일 종가)로는 실제로 살 수 없다 — 그 가격으로 진입한 것처럼
    기록하면 성과가 부풀거나 꺼진다(look-ahead). 그래서 체결을 개장까지 미루고,
    개장가와 신호가의 괴리가 크면 아예 버린다.
    """
    import pending_orders as po

    orders = await asyncio.to_thread(po.load)
    if not orders:
        return []

    # 대기 주문 체결도 매수다 — 폴백 금지(일봉 종가로 채우면 대기시킨 의미가 없다).
    tickers = [o.get("ticker") for o in orders]
    fetched = await asyncio.gather(
        *(asyncio.to_thread(_get_execution_price, t) for t in tickers)
    )
    prices: dict = dict(zip(tickers, fetched))

    results = po.revalidate_all(orders, prices, now.date())
    slot_ids = {s["name"]: s["id"] for s in await asyncio.to_thread(_pdb.list_slots)}
    cycle_id = now.strftime("%Y%m%dT%H%M")

    for i, r in enumerate(results):
        if r["verdict"] != "fill":
            log.info("대기 주문 취소 %s — %s", r.get("ticker"), r["reason"])
            continue
        slot_id = slot_ids.get(r.get("slot"))
        if slot_id is None:
            r["verdict"] = "cancel"
            r["reason"] = f"슬롯 '{r.get('slot')}'을 찾지 못함"
            continue
        # 한도에 걸린 슬롯이면 대기 주문도 들어가지 않는다
        blocked = _slot_hard_stop_reason(r.get("slot"))
        if blocked:
            r["verdict"] = "cancel"
            r["reason"] = "슬롯 일일 손실 한도로 신규 매수 차단"
            continue
        o = r["order"]
        notes = f"[AUTO] {o.get('source', '봇')} 대기체결 (신호 {o.get('signal_at', '')[:16]}, 갭 {r['gap']:+.2f}%)"
        try:
            import entry_tags
            notes = entry_tags.format_note(notes, r.get("tags"))
        except Exception:
            log.warning("대기 체결 태그 생성 실패", exc_info=True)
        try:
            if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                await asyncio.to_thread(
                    _direct_paper_write,
                    _pdb.record_buy, int(slot_id), r["ticker"], r.get("name") or r["ticker"],
                    int(r["qty"]), float(r["price"]), notes,
                )
            else:
                await _execute_private_paper_write(
                    caller="telegram-pending",
                    action="buy",
                    slot_id=int(slot_id),
                    actor_id="pending-queue",
                    source_event_id=cycle_id,
                    item_key=f"pending-{i}",
                    ticker=r["ticker"],
                    name=r.get("name"),
                    quantity=int(r["qty"]),
                    price=float(r["price"]),
                    notes=notes,
                    user_approved=True,      # 장외 배치 승인 시 이미 사람이 승인했다
                    policy_approved=False,
                )
        except Exception as exc:  # noqa: BLE001
            r["verdict"] = "cancel"
            r["reason"] = f"체결 실패: {exc}"
            log.warning("대기 주문 체결 실패 %s: %s", r.get("ticker"), exc)

    # 처리한 것은 성공·실패와 무관하게 큐에서 비운다 — 남겨 두면 다음 사이클에 또 시도한다
    await asyncio.to_thread(po.clear)
    text = po.format_results(results)
    return [text] if text else []


def _equity_weight_now() -> tuple[float, str]:
    """지금 쓸 주식 목표 비중과 그 근거(한 줄).

    `kium_bot.compute_weight_recommendation`은 KOSPI 200일선·VKOSPI로 70/50%를
    낸다. **v3.59 전에는 이 값을 매수 경로 어디에서도 읽지 않았다** — 출력
    문구에만 쓰였다. 여기서 실제 예산으로 옮긴다.

    지수 시계열이 없으면 함수가 `reason='데이터 부족'`과 함께 기본값 0.70을
    돌려준다. **그 사실을 삼키지 않고 근거 문구에 남긴다** — '판단해서 70%'와
    '몰라서 70%'는 다르다.
    """
    import slot_budget as _sb
    try:
        import kium_bot as _kb
        import proxy_indicators as _pi

        # 지수 시계열은 `proxy_indicators._kospi_series`와 **같은 캐시**를 쓴다
        # (`cache/indices/market_index_KOSPI_*.json`). 처음엔 종목 일봉 캐시에서
        # "1001"을 찾다 0건이 나와 늘 '데이터 부족'이었다 — 지수는 거기 없다.
        closes, as_of = _pi._kospi_series()
        rec = _kb.compute_weight_recommendation(
            kospi_close=closes if len(closes) >= 200 else None)
        w = float(rec.get("equity_weight") or _sb.DEFAULT_EQUITY_WEIGHT)
        why = str(rec.get("reason") or "")
        if closes and as_of:
            why = f"{why} · 지수 {as_of} 기준 {len(closes)}일"
        return w, why
    except Exception:
        log.warning("주식 비중 권고 조회 실패 — 기본값 사용", exc_info=True)
        return _sb.DEFAULT_EQUITY_WEIGHT, "권고 조회 실패"


def _equity_budget(cash, positions, new_names, *, source: str = "") -> tuple[float, dict]:
    """신규 1종목당 예산과 근거. 실패하면 옛 방식(현금 ÷ 종목수)으로 물러난다."""
    import slot_budget as _sb
    n = len(new_names or [])
    try:
        hv = _sb.holdings_value(positions or [])
        w, why = _equity_weight_now()
        plan = _sb.budget_for_new(float(cash or 0), hv["value"], n, equity_weight=w)
        # **초과 상태를 이름으로 부른다.** 예산이 0이면 수량도 0이 되어 매수가
        # 조용히 사라지고, 화면에는 "배정금액 부족"으로 뜬다 — 원인이 아니라
        # 증상이다. 목표를 넘어서 안 사는 것과 돈이 모자라 못 사는 것은 다르다.
        import equity_drift as _ed

        drift = _ed.assess(float(cash or 0), hv["value"], w)
        log.info(
            f"{source} 예산: 슬롯총액 "
            f"{_sb.slot_total(float(cash or 0), hv['value']):,.0f}원 "
            f"(현금 {float(cash or 0):,.0f} + 보유 {hv['value']:,.0f} · {hv['basis']}) · "
            f"목표 주식 {w * 100:.0f}%"
            f"{f'({why})' if why else ''} → 신규 {n}종목 × {plan['per_name']:,.0f}원")
        return plan["per_name"], {"plan": plan, "holdings": hv, "drift": drift,
                                  "weight": w, "reason": why}
    except Exception:
        log.warning("예산 산정 실패 — 옛 방식으로 진행", exc_info=True)
        return (float(cash or 0) / n if n else 0.0), {}


def _apply_second_pass(resolved: dict, alloc_per: float, budget: dict,
                       *, source: str = "") -> None:
    """정수 주식수 내림으로 남은 예산을 **싼 종목부터** 한 주씩 더 담는다.

    비싼 종목일수록 버림이 크다 — 2026-08-31 배치에서 SK하이닉스는 1주가 예산의
    51.7%라 나머지 48%가 그냥 남았고, 전체로 2,592,350원이 미집행됐다.
    한 종목에 몰아 담지 않도록 **싼 순서로 한 바퀴에 한 주씩** 돌린다.

    `resolved`를 제자리에서 고친다. 실패해도 1차 배분 결과는 그대로 쓴다.
    """
    import slot_budget as _sb
    plan = (budget or {}).get("plan") or {}
    spendable = float(plan.get("spendable") or 0)
    if not resolved or spendable <= 0:
        return
    try:
        qty = {t: int(v.get("qty") or 0) for t, v in resolved.items()}
        prices = {t: float(v.get("price") or 0) for t, v in resolved.items()}
        spent = sum(qty[t] * prices[t] for t in qty)
        # 체결된 종목만 남았을 수 있으므로 실제 집행액 기준으로 잔액을 다시 센다.
        top = _sb.second_pass_topup(qty, prices, max(0.0, spendable - spent))
        if not top["added"]:
            return
        for ticker, extra in top["added"].items():
            resolved[ticker]["qty"] = top["quantities"][ticker]
        log.info(f"{source} 2차 배분: "
                 + ", ".join(f"{t} +{n}주" for t, n in top["added"].items())
                 + f" — {top['spent']:,.0f}원 추가 집행")
    except Exception:
        log.warning("2차 배분 실패 — 1차 결과로 진행", exc_info=True)


# v3.60 — 목표 주식 비중 추종 점검 (콴텍·키움)
EQUITY_DRIFT_SLOTS = ("콴텍", "키움")
EQUITY_DRIFT_INTERVAL_SEC = 60 * 60 * 6      # 6시간마다 검사(알림은 상태가 바뀔 때만)
EQUITY_DRIFT_STATE_FILE = REBALANCE_FLAG_DIR / "equity_drift_state.json"


def _fill_price_stale_block(resolved: dict) -> str:
    """**체결가**가 오래된 종가인지 본다. 아니면 빈 문자열.

    스캔가 관문(`check_batch`)은 "신호가 언제 것인가"를 보고, 이쪽은 **실제로
    기록될 가격**을 본다. 2026-06-08 사고에서 손실을 만든 것은 스캔가가 아니라
    체결가였다.

    체결가는 `_get_execution_price`(네이버 실시간 전용, 폴백 없음)에서 온다.
    그래도 한 번 더 보는 이유는, 실시간 소스가 장 시작 전 값을 그대로 돌려주는
    경우를 코드로는 구분할 수 없기 때문이다. **한 배치의 여러 종목이 같은 과거
    날짜 종가와 원 단위까지 일치하면** 그건 우연이 아니다.
    """
    import price_sanity as _ps

    fills = [{"ticker": t, "name": v.get("name") or t,
              "price": float(v.get("price") or 0)}
             for t, v in (resolved or {}).items() if v.get("price")]
    if len(fills) < _ps.MIN_BATCH_HITS:
        return ""
    try:
        from datetime import datetime as _dt

        verdict = _ps.check_batch(fills, _now_kst().strftime("%Y%m%d"))
    except Exception:
        log.warning("체결가 스테일 점검 실패 — 통과시킴", exc_info=True)
        return ""
    if not verdict.get("stale"):
        return ""
    return _ps.format_block(verdict)


def _equity_drift_snapshot() -> tuple[dict, float, str]:
    """콴텍·키움의 현재 비중 판정. (결과, 목표비중, 근거)"""
    import equity_drift as _ed
    import slot_budget as _sb

    weight, why = _equity_weight_now()
    out: dict = {}
    slots = _pdb.list_slots()
    for slot in slots:
        name = str(slot.get("name") or "")
        if name not in EQUITY_DRIFT_SLOTS:
            continue
        positions = _pdb.list_positions(slot["id"])
        prices = {}
        for pos in positions:
            px = _get_execution_price(str(pos.get("ticker")))
            if px:
                prices[str(pos.get("ticker"))] = float(px)
        hv = _sb.holdings_value(positions, prices)
        out[name] = _ed.assess(float(slot.get("current_capital") or 0),
                               hv["value"], weight)
        out[name]["basis"] = hv["basis"]
    return out, weight, why


async def equity_drift_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """목표 주식 비중을 계속 따라가는지 점검한다(콴텍·키움).

    **매매하지 않는다.** 상태만 보고 사람에게 알린다. 초과여도 강제 매도하지
    않는다 — 되돌릴 수 없는 조치는 사람 판단에 남긴다(슬롯 일일 손실 한도와
    같은 원칙). 초과 상태의 신규 매수 차단은 리밸런싱 경로에서 이미 걸린다.

    **상태가 바뀔 때만 보낸다.** 매일 같은 말을 보내면 사람은 그 알림을 안 보게
    되고, 정작 바뀌었을 때도 놓친다.
    """
    import json as _json

    import equity_drift as _ed

    now = _now_kst()
    if now.weekday() >= 5:
        return
    try:
        results, weight, why = await asyncio.to_thread(_equity_drift_snapshot)
    except Exception:
        log.warning("목표 비중 점검 실패", exc_info=True)
        return
    if not results:
        return

    key = _ed.state_key(results, weight)
    previous = None
    try:
        previous = _json.loads(
            EQUITY_DRIFT_STATE_FILE.read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError):
        pass

    for name, r in sorted(results.items()):
        log.info("목표 비중 점검 %s: %s (%s)", name, r["detail"], r.get("basis", ""))
    if key == previous:
        return

    try:
        EQUITY_DRIFT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        EQUITY_DRIFT_STATE_FILE.write_text(
            _json.dumps({"key": key, "at": now.isoformat(timespec="seconds")},
                        ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("목표 비중 상태 저장 실패 — 다음에 또 알릴 수 있다: %s", e)

    text = _ed.format_report(results, weight, why)
    for uid in ALLOWED_IDS:
        try:
            await ctx.bot.send_message(chat_id=int(uid), text=text,
                                       parse_mode="Markdown")
        except Exception:
            log.warning("목표 비중 알림 발송 실패 user=%s", uid, exc_info=True)


def _equity_block_line(budget: dict) -> str:
    """목표 비중 초과로 신규 매수를 막을 때의 한 줄. 아니면 빈 문자열.

    **초과와 현금 부족은 다르다.** 예산이 0이면 수량이 0이 되어 매수가 조용히
    사라지고 화면에는 "배정금액 부족"으로 뜬다 — 증상이지 원인이 아니다.
    """
    import equity_drift as _ed

    drift = (budget or {}).get("drift")
    if not drift or not _ed.blocks_new_buys(drift):
        return ""
    return (f"⚖️ 주식 비중 {drift['current']*100:.1f}% > 목표 "
            f"{drift['target']*100:.0f}% — 신규 매수를 건너뜁니다"
            f"(기존 보유는 그대로 둡니다)")


IDLE_CASH_SLOT = "IPO"
IDLE_CASH_INTERVAL_SEC = 60 * 60 * 4     # 장중 4시간 간격(하루 2회 남짓)


async def idle_cash_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """IPO 슬롯 유휴 자본을 단기통안채로 파킹/현금화 (평일 09:30~15:00).

    2026-08-29 실측: IPO 슬롯 2,000만원이 개설(05-10) 이후 **3.5개월간 거래 0건**
    이었다. 슬롯 자본은 성과 집계에 잡히는데 수익은 0이라, 유휴 자본이 전체
    수익률을 구조적으로 끌어내린다. IPO는 간헐적이므로 이 상태가 기본값이 된다.

    판단은 `idle_cash`(순수)가 하고 여기서는 상태를 모아 넘기고 결과를 실행만
    한다. 청약 일정은 **DART 보강 없이** 38·KIND 목록만 쓴다 — 필요한 것은
    날짜뿐이라 종목당 문서 3건을 받을 이유가 없다.
    """
    now = _now_kst()
    if now.weekday() >= 5:
        return
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if not (start <= now <= end):
        return

    try:
        slots = await asyncio.to_thread(_pdb.list_slots)
        slot = next((s for s in slots if s.get("name") == IDLE_CASH_SLOT), None)
        if slot is None:
            return
        positions = await asyncio.to_thread(_pdb.list_positions, slot["id"])
        parked = next((p for p in positions
                       if str(p.get("ticker")) == idle_cash.PARK_TICKER), None)
        parked_qty = int(parked["quantity"]) if parked else 0

        items = await asyncio.to_thread(fetch_ipo_schedule, 30)
        subs = [it.sub_start for it in items]

        price = None
        if parked_qty == 0:
            price = await asyncio.to_thread(
                _get_execution_price, idle_cash.PARK_TICKER)

        plan = idle_cash.plan_idle_action(
            parked_qty=parked_qty,
            cash=float(slot.get("current_capital") or 0),
            subscriptions=subs,
            today=now.date(),
            price=price,
        )
    except Exception:
        log.warning("유휴 자본 판단 실패", exc_info=True)
        return

    if plan["action"] == "hold":
        # **데이터 때문에 못 움직이는 것은 debug에 묻으면 안 된다.** 2026-08-31:
        # 청약일 미확정을 "못 읽음"으로 세는 버그로 2,000만원이 계속 유휴 상태였는데,
        # 사유가 debug 로그에만 있어 이틀 동안 아무도 몰랐다.
        if idle_cash.unreadable_subscriptions(subs):
            log.warning("유휴 자본 보류(데이터 문제): %s", plan["reason"])
        else:
            # 4시간마다 도는 잡이라 장중 2회다 — INFO로 남겨도 소음이 아니고,
            # **왜 계속 현금인지**를 로그만 보고 알 수 있어야 한다(2026-08-31:
            # 파킹이 안 되는 이유를 debug 때문에 확인할 수 없었다).
            log.info("유휴 자본 보류: %s", plan["reason"])
        return

    # 매도 시점의 체결가는 따로 받는다(판단은 수량만 정한다).
    exec_price = price
    if plan["action"] == "sell":
        exec_price = await asyncio.to_thread(
            _get_realtime_price, idle_cash.PARK_TICKER)
    if not exec_price or exec_price <= 0:
        log.warning("유휴 자본: %s 시세 미확보 — 실행 보류", idle_cash.PARK_TICKER)
        return

    notes = f"[AUTO] 유휴자본 {plan['action']} — {plan['reason']}"
    try:
        if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
            fn = _pdb.record_buy if plan["action"] == "buy" else _pdb.record_sell
            args = ((int(slot["id"]), idle_cash.PARK_TICKER, idle_cash.PARK_NAME,
                     plan["qty"], float(exec_price))
                    if plan["action"] == "buy"
                    else (int(slot["id"]), idle_cash.PARK_TICKER,
                          plan["qty"], float(exec_price)))
            await asyncio.to_thread(_direct_paper_write, fn, *args, notes=notes)
        else:
            await _execute_private_paper_write(
                caller="telegram-idle-cash",
                action=plan["action"],
                slot_id=int(slot["id"]),
                ticker=idle_cash.PARK_TICKER,
                name=idle_cash.PARK_NAME,
                quantity=plan["qty"],
                price=float(exec_price),
                notes=notes,
                user_approved=False,
                policy_approved=True,
            )
    except Exception:
        log.warning("유휴 자본 실행 실패", exc_info=True)
        return

    text = idle_cash.format_plan(plan, slot=IDLE_CASH_SLOT)
    log.info("유휴 자본: %s @ %s원", text, f"{exec_price:,.0f}")
    for uid in ALLOWED_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=f"💤 {text}\n체결가 {exec_price:,.0f}원")
        except Exception:
            pass


async def intraday_monitor_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """평일 09:05~15:30 사이 5분 간격으로 실행.

    전 슬롯 보유 포지션을 네이버 실시간 현재가(pykrx 폴백)로 체크,
    exit_rules.should_exit 판정(급락 하드스톱/손절선/익절선/트레일링)으로
    자동 청산 + 텔레그램 알림. 트레일링 피크는 json 캐시로 재시작에도 유지.
    장외 시간대에는 즉시 리턴.
    """
    now = _now_kst()  # v3.46 — 서버 타임존 무관 KST 게이트

    # 평일(월~금)만
    if now.weekday() >= 5:
        return

    market_open = now.replace(hour=9, minute=5, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        return

    # v3.50 — 대기 주문 먼저. 보유 포지션이 없어도 체결해야 하므로 아래 조기 반환보다 앞에 둔다.
    pending_alerts: list[str] = []
    try:
        pending_alerts = await _drain_pending_orders(ctx, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("대기 주문 처리 실패: %s", exc)

    positions = await asyncio.to_thread(_list_runtime_paper_positions)
    if not positions:
        if pending_alerts:
            for uid in ALLOWED_IDS:
                try:
                    await ctx.bot.send_message(chat_id=uid, text="\n\n".join(pending_alerts),
                                               parse_mode="Markdown")
                except Exception as e:
                    log.warning(f"대기 주문 알림 실패 (uid={uid}): {e}")
        return

    log.info(
        f"🔍 장 중 모니터 — {len(positions)}개 포지션 체크 ({now.strftime('%H:%M')})"
    )

    state = _load_intraday_state()
    peaks, last_px = state["peaks"], state["px"]
    live_keys: set = set()
    alerts: list[str] = list(pending_alerts)

    # v3.45 — 유효 포지션 시세 병렬 조회(네이버 1차) / v3.46 — 시세원 건강 감시
    valid = [
        p for p in positions if int(p["quantity"]) > 0 and float(p["avg_price"]) > 0
    ]
    naver_prices = await asyncio.gather(
        *(asyncio.to_thread(_get_naver_price, p["ticker"]) for p in valid)
    )
    n_naver_ok = sum(1 for x in naver_prices if x)
    raw_prices = list(naver_prices)
    for i, (p, nv) in enumerate(zip(valid, naver_prices)):
        if not nv:  # 네이버 실패분만 pykrx 폴백
            raw_prices[i] = await asyncio.to_thread(_get_pykrx_price, p["ticker"])

    streak, warn = exit_rules.naver_health(
        n_naver_ok, len(valid), int(state.get("naver_streak") or 0)
    )
    if warn:
        alerts_health = (
            f"⚠️ *실시간 시세원 이상* — 네이버 시세가 {streak}사이클 연속 전멸, "
            f"pykrx 종가 폴백으로 동작 중입니다(손절 실행 지연 재발 위험). "
            f"네트워크/API 응답 형식을 확인하세요."
        )
    else:
        alerts_health = None
    if n_naver_ok < len(valid):
        log.info(
            f"  시세원: 네이버 {n_naver_ok}/{len(valid)} 성공 "
            f"(전멸 연속 {streak}회)"
        )

    new_px: dict[str, float] = {}
    cycle_id = now.strftime("%Y%m%dT%H%M")
    for position_index, (pos, raw_price) in enumerate(zip(valid, raw_prices)):
        ticker = pos["ticker"]
        qty = int(pos["quantity"])
        avg_price = float(pos["avg_price"])
        slot_id = int(pos["slot_id"])
        slot_name = pos.get("slot_name", "?")
        name = pos.get("name") or ticker

        # v3.45 — 오호가 방어: 직전 폴링가 대비 급변 시 pykrx 교차확인
        cur_price = exit_rules.confirm_price(
            raw_price, last_px.get(ticker), lambda t=ticker: _get_pykrx_price(t)
        )
        if cur_price is None:
            if raw_price:
                log.warning(
                    f"  {ticker}: 의심 틱 {raw_price:,.0f}원 "
                    f"(직전 {last_px.get(ticker)}) — 이번 사이클 스킵"
                )
            else:
                log.debug(f"  {ticker}: 가격 조회 실패 — 스킵")
            key = exit_rules.peak_key(slot_id, ticker, avg_price)
            if key in peaks:
                live_keys.add(key)  # 조회 실패해도 피크 유지
            continue
        new_px[ticker] = cur_price

        pnl_pct = (cur_price - avg_price) / avg_price
        key = exit_rules.peak_key(slot_id, ticker, avg_price)
        peak = exit_rules.update_peak(peaks, key, pnl_pct)
        verdict = exit_rules.should_exit(pnl_pct, peak)

        if verdict is None:
            live_keys.add(key)
            continue

        action, reason = verdict
        emoji = "🚨" if action == "손절" else "🎯"
        try:
            notes = f"{action} 자동청산[{reason}] ({pnl_pct*100:.1f}%)"
            if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                _direct_paper_write(
                    _pdb.record_sell,
                    slot_id,
                    ticker,
                    qty,
                    cur_price,
                    notes=notes,
                )
            else:
                await _execute_private_paper_write(
                    caller="telegram-intraday",
                    action="sell",
                    slot_id=slot_id,
                    actor_id="intraday-policy",
                    source_event_id=cycle_id,
                    item_key=f"position-{position_index}",
                    ticker=ticker,
                    quantity=qty,
                    price=cur_price,
                    notes=notes,
                    user_approved=False,
                    policy_approved=True,
                )
            log.info(
                f"{emoji} {action}청산[{reason}] {ticker} [{slot_name}] "
                f"{pnl_pct*100:.1f}% @ {cur_price:,.0f}원"
            )
            alerts.append(
                f"{emoji} *{action}청산* ({reason}) {name} ({ticker}) [{slot_name}]\n"
                f"  평균가 {avg_price:,.0f} → {cur_price:,.0f}원 "
                f"({pnl_pct*100:+.1f}%) | {qty}주 전량"
            )
        except Exception as e:
            log.warning(f"{action} 기록 실패 {ticker}: {e}")
            live_keys.add(key)  # 청산 실패 → 피크 유지

    if alerts_health:
        alerts.append(alerts_health)

    # v3.48 — 슬롯 일일 손실 한도. 청산은 하지 않고 신규 매수만 막는다.
    # 판정·상태 저장은 여기(장중 모니터)가 소유하고, 매수 경로는 상태만 읽는다.
    try:
        import slot_hard_stop
        _, _, fresh_stops = await asyncio.to_thread(slot_hard_stop.check_and_record)
        stop_alert = slot_hard_stop.format_alert(fresh_stops)
        if stop_alert:
            alerts.append(stop_alert)
    except Exception as exc:  # noqa: BLE001
        log.warning("슬롯 일일 한도 점검 실패: %s", exc)

    _save_intraday_state(
        {
            "peaks": exit_rules.prune_peaks(peaks, live_keys),
            "px": new_px,
            "naver_streak": streak,
        }
    )

    if alerts:
        msg = "\n\n".join(alerts)
        for uid in ALLOWED_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=msg,
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.warning(f"모니터 알림 실패 (uid={uid}): {e}")


async def technical_signal_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """평일 09:00~15:30, 1시간 간격. 핵심 자산배분 워치리스트 기술적 신호 푸시.

    signal_bot.scan() (1시간봉 yfinance) → actionable 신호만 추출.
    같은 (종목·전략·액션) 신호는 당일 1회만 전송(멱등, signal_last.json).
    장외/주말 즉시 리턴. 실주문 없음 — 알림만.
    """
    now = _now_kst()  # v3.46 — 서버 타임존 무관 KST 게이트
    if now.weekday() >= 5:
        return
    open_t = now.replace(
        hour=SIGNAL_OPEN_HOUR, minute=SIGNAL_OPEN_MIN, second=0, microsecond=0
    )
    close_t = now.replace(
        hour=SIGNAL_CLOSE_HOUR, minute=SIGNAL_CLOSE_MIN, second=0, microsecond=0
    )
    if not (open_t <= now <= close_t):
        return

    try:
        _msg, sigs = await asyncio.to_thread(signal_bot.run)
    except Exception as e:
        log.warning(f"기술적 신호 스캔 실패: {e}")
        return
    if not sigs:
        return

    # 멱등: 당일 이미 보낸 신호 키 제외 (signal_bot 순수 함수 사용)
    today = now.strftime("%Y-%m-%d")
    sent = signal_bot.load_sent_keys(_SIGNAL_DEDUP_PATH, today)
    fresh, sent = signal_bot.filter_new_signals(sigs, sent)
    if not fresh:
        return
    signal_bot.save_sent_keys(_SIGNAL_DEDUP_PATH, today, sent)

    msg = signal_bot.format_signals(fresh)
    if not msg:
        return
    log.info(f"📡 기술적 신호 {len(fresh)}건 푸시 ({now.strftime('%H:%M')})")
    for uid in ALLOWED_IDS:
        try:
            await ctx.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"신호 알림 실패 (uid={uid}): {e}")


# ─── v3.43 봇작업 예약(action_schedule) 실행 인프라 ────


def _run_action(action: str) -> str:
    """예약 액션 키 → 실제 봇 실행 결과 메시지."""
    if action == "news":
        return news_run()[0] or "신규 기사 없음"
    if action == "signal":
        return signal_bot.run()[0] or "현재 actionable 신호 없음"
    if action == "ipo":
        return ipo_run(action="scan")[0]
    if action == "quant":
        return quant_run(action="phase")[0]
    if action == "performance":
        # v3.46 — 리포트 생성 시 성과 스냅샷도 적재(EWMA 평활·추세 입력 누적)
        try:
            import paper_db as _pdb_perf

            _stats = _pdb_perf.performance_stats()
            try:
                _panalytics.record_snapshot(_stats)
            except Exception as _e:
                log.warning(f"성과 스냅샷 적재 실패 — 리포트는 계속: {_e}")
            return _panalytics.format_report(_stats)
        except Exception:
            return _panalytics.report()
    return f"알 수 없는 액션: {action}"


# 코드에 등록된 시스템 고정 작업(JobQueue) — 목록에 함께 표시(읽기 전용)
_BUILTIN_JOBS = [
    "주간 스캔(키움 모멘텀) · 매주 월 09:05",
    "주간 스캔(IPO 매력지수) · 매주 월 09:10",
    "월간 콴텍 리밸런싱 추천 · 매월 첫 영업일 09:30",
    "장중 손절·익절 모니터(paper) · 평일 09:05~15:30 30분",
    "장중 기술적 신호(1시간봉) · 평일 09:00~15:30 1시간",
    "일정 리마인더 알림 · 60초 검사",
]


def _full_automation_list() -> str:
    parts = [_asch.format_list()]
    parts.append("\n⚙️ 시스템 고정 작업(코드 등록, 읽기 전용)")
    for j in _BUILTIN_JOBS:
        parts.append(f"• {j}")
    return "\n".join(parts)


def _handle_action_schedule(args: dict) -> str:
    """라우터가 넘긴 op(add/list/delete/disable/enable) 처리 → 사용자 응답."""
    op = args.get("op")
    if op == "list":
        return _full_automation_list()
    if op == "delete":
        ok = _asch.delete_schedule(int(args["id"]))
        return (
            f"🗑 자동작업 #{args['id']} 삭제됨"
            if ok
            else f"#{args['id']} 작업을 못 찾았습니다."
        )
    if op in ("disable", "enable"):
        ok = _asch.set_enabled(int(args["id"]), op == "enable")
        state = "중지" if op == "disable" else "재개"
        return (
            f"⏸ 자동작업 #{args['id']} {state}됨"
            if ok
            else f"#{args['id']} 작업을 못 찾았습니다."
        )
    if op == "add":
        sched = _asch.add_schedule(
            args["action"],
            args.get("freq", "daily"),
            args["time"],
            weekday=args.get("weekday"),
            until=args.get("until"),
        )
        return (
            "✅ 자동작업 등록\n"
            + sched.describe()
            + "\n(이 시각에 제가 직접 실행해 보냅니다)"
        )
    return "예약 명령을 이해하지 못했습니다."


def _seed_default_schedules() -> None:
    """최초 1회: 비어 있으면 뉴스 다이제스트 기본값(매일 08:00, 2026-06-11까지) 시드."""
    try:
        if not _asch.load_schedules():
            _asch.add_schedule("news", "daily", "08:00", until="2026-06-11")
            log.info("🌱 기본 자동작업 시드: 뉴스 매일 08:00 (~2026-06-11)")
    except Exception as e:
        log.warning(f"기본 스케줄 시드 실패: {e}")


async def action_dispatch_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """60초마다 호출 — 지금 시각에 발화할 예약작업을 실행·전송(멱등)."""
    from datetime import datetime as _dt

    now = _dt.now()
    try:
        due = _asch.due_now(now)
    except Exception as e:
        log.warning(f"예약 조회 실패: {e}")
        return
    for sched in due:
        try:
            msg = await asyncio.to_thread(_run_action, sched.action)
        except Exception as e:
            log.warning(f"예약작업 #{sched.id} 실행 실패: {e}")
            _asch.mark_fired(sched.id, now)
            continue
        _asch.mark_fired(sched.id, now)
        if not msg:
            continue
        log.info(f"⏰ 예약작업 #{sched.id} {sched.action} 발송")
        for uid in ALLOWED_IDS:
            try:
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning(f"예약 발송 실패 (uid={uid}): {e}")


async def news_digest_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """run_daily가 매일 08:00(KST) 정확히 1회 호출 → IT/AI 뉴스 다이제스트 푸시."""
    from datetime import datetime as _dt

    today = _dt.now().strftime("%Y-%m-%d")
    if NEWS_DIGEST_UNTIL and today > NEWS_DIGEST_UNTIL:
        return  # 종료일 경과 — 발송 중단
    # run_daily가 하루 1회만 호출하므로 폴링·플래그 불필요.
    try:
        msg, arts = await asyncio.to_thread(news_run)
    except Exception as e:
        log.warning(f"뉴스 다이제스트 실패: {e}")
        return
    if not msg:
        return
    log.info(f"📰 뉴스 다이제스트 푸시 ({len(arts)}건)")
    for uid in ALLOWED_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=uid,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning(f"뉴스 알림 실패 (uid={uid}): {e}")


async def notify_due_events(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue가 NOTIFY_INTERVAL_SEC마다 호출.

    schedule.db에서 발화 임박/기한 도래한 일정 (메인 + 사전 알림)을 찾아
    chat_id로 push. 각 알림 후 멱등 플래그 설정. 반복 일정은 메인 발송 후
    when_at을 다음 발화 시각으로 자동 갱신.
    """
    try:
        from schedule_bot import (
            due_for_notification,
            mark_notified,
            mark_pre_notified,
            advance_recurring,
        )

        events = await asyncio.to_thread(
            due_for_notification, horizon_seconds=NOTIFY_HORIZON_SEC
        )
    except Exception as e:
        log.exception(f"알림 스캔 실패: {e}")
        return

    if not events:
        return

    n_main = sum(1 for e in events if e.get("kind") == "main")
    n_pre = sum(1 for e in events if e.get("kind") == "pre")
    log.info(f"🔔 알림 발송: 메인 {n_main}건 / 사전 {n_pre}건")

    for ev in events:
        cid = ev.get("chat_id")
        if not cid:
            continue
        kind = ev.get("kind", "main")
        try:
            if kind == "pre":
                # v3.15: 멀티 사전알림 — minutes_before가 specific alert 값.
                # 없으면 legacy events.pre_notify_minutes로 fallback.
                pre_min = ev.get("minutes_before") or ev.get("pre_notify_minutes", 0)
                text = (
                    f"🔔 일정 사전 알림 ({pre_min}분 전)\n"
                    f"📌 {ev['title']}\n"
                    f"📅 {ev['when_pretty']}"
                    + (f"\n📝 {ev['notes']}" if ev.get("notes") else "")
                )
                await ctx.bot.send_message(chat_id=int(cid), text=text)
                # v3.15: 그 alert만 마킹 (다른 alert는 별도 발송 대기). minutes_before 없으면 legacy로 전체 마킹.
                if ev.get("minutes_before") is not None:
                    await asyncio.to_thread(
                        mark_pre_notified, ev["id"], int(ev["minutes_before"])
                    )
                else:
                    await asyncio.to_thread(mark_pre_notified, ev["id"])
            else:
                rep_line = ""
                if ev.get("rrule_freq"):
                    rep_line = f"\n🔁 반복: {ev['rrule_freq']}"
                    if ev.get("rrule_byday"):
                        rep_line += f" ({ev['rrule_byday']})"
                text = (
                    f"⏰ 일정 알림\n"
                    f"📌 {ev['title']}\n"
                    f"📅 {ev['when_pretty']}"
                    + (f"\n📝 {ev['notes']}" if ev.get("notes") else "")
                    + rep_line
                    + f"\n\n#{ev['id']} 완료 처리: '#{ev['id']} 완료'"
                )
                await ctx.bot.send_message(chat_id=int(cid), text=text)
                await asyncio.to_thread(mark_notified, ev["id"])
                # 반복 일정이면 다음 발화 시각으로 자동 진행
                if ev.get("rrule_freq"):
                    nxt = await asyncio.to_thread(advance_recurring, ev["id"])
                    if nxt:
                        log.info(f"🔁 #{ev['id']} 다음 발화: {nxt}")
                    else:
                        log.info(f"🔁 #{ev['id']} 반복 종료 (until 도달)")
        except Exception as e:
            log.error(f"알림 발송 실패 #{ev['id']} kind={kind} chat={cid}: {e}")


# ─── 콴텍봇 월간 리밸런싱 자동 푸시 (v3.23) ─────────


def _first_business_day_passed(today: "datetime") -> bool:
    """이번 달 첫 영업일이 (오늘 이전에) 이미 지났는지 — catch-up 판정용."""
    try:
        from pykrx import stock

        first = today.replace(day=1)
        biz = stock.get_previous_business_days(
            fromdate=first.strftime("%Y%m%d"), todate=today.strftime("%Y%m%d")
        )
        if not biz:
            return False
        fb = biz[0]
        fb_str = fb.strftime("%Y%m%d") if hasattr(fb, "strftime") else str(fb)
        return today.strftime("%Y%m%d") > fb_str
    except Exception as e:
        log.warning(f"pykrx 영업일(passed) 조회 실패 — fallback: {e}")
        # fallback: 7일 이후면 첫 영업일은 확실히 지남(공휴일 무시 근사)
        return today.day > 7


def is_first_business_day(today: "datetime") -> bool:
    """오늘이 KRX 첫 영업일인지 — pykrx 영업일 캘린더 기반 (없으면 weekday fallback)."""
    try:
        from pykrx import stock

        # 같은 달 1일~오늘 사이 영업일 list
        first = today.replace(day=1)
        biz_days = stock.get_previous_business_days(
            fromdate=first.strftime("%Y%m%d"),
            todate=today.strftime("%Y%m%d"),
        )
        if not biz_days:
            return False
        # biz_days[0] == 첫 영업일
        first_biz = biz_days[0]
        # pandas Timestamp / str 둘 다 핸들
        first_str = (
            first_biz.strftime("%Y%m%d")
            if hasattr(first_biz, "strftime")
            else str(first_biz)
        )
        return first_str == today.strftime("%Y%m%d")
    except Exception as e:
        log.warning(f"pykrx 영업일 조회 실패 — weekday fallback: {e}")
        # Fallback: 1~7일 사이 + 평일 (Mon~Fri). 정확도는 떨어짐 (공휴일 무시).
        return today.day <= 7 and today.weekday() < 5


def _load_rebalance_flag() -> dict:
    """{"YYYY-MM": {"pushed": [], "executed": [], "pushes": {}}} 영속화.

    v3.59: 옛 형식 `{"YYYY-MM": [uid]}`도 읽어 올린다. **옛 항목은 "푸시됨"까지만
    아는 것**이므로 실행 여부는 비워 둔다 — 모르는 것을 안다고 적지 않는다.
    """
    import rebalance_pending as _rp
    try:
        if REBALANCE_FLAG_FILE.exists():
            import json as _json

            return _rp.normalize_flag(
                _json.loads(REBALANCE_FLAG_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning(f"리밸런싱 플래그 읽기 실패 — fresh start: {e}")
    return {}


def _save_rebalance_flag(data: dict) -> None:
    try:
        REBALANCE_FLAG_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json

        REBALANCE_FLAG_FILE.write_text(
            _json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"리밸런싱 플래그 쓰기 실패 — 무시: {e}")


async def quant_monthly_rebalance(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """24h 주기 호출. 매월 첫 영업일 09:30 직후만 ALLOWED_IDS chat_id별 콴텍봇 추천 푸시.

    멱등성: REBALANCE_FLAG_FILE에 "YYYY-MM" 키별 발송 완료 user_id list 저장.
    같은 달 같은 user는 중복 발송 안 함.
    """
    from datetime import datetime as _dt

    now = _dt.now()

    import rebalance_pending as _rp

    month_key = now.strftime("%Y-%m")
    flag = _load_rebalance_flag()

    # v3.59: "푸시했다"가 아니라 **"실행됐다"**를 기준으로 본다. 2026-07은 푸시만
    # 되고 아무도 버튼을 누르지 않았는데 플래그가 처리된 달로 보여 다시 알리지
    # 않았고, 그 달 진입이 통째로 사라졌다(그동안 자동청산은 계속 돌았다).
    pending = [uid for uid in ALLOWED_IDS
               if _rp.needs_push(flag, month_key, uid, now.date())]
    if not pending:
        return

    for uid in ALLOWED_IDS:
        stuck = [m for m in _rp.unexecuted_months(flag, uid) if m != month_key]
        if stuck:
            log.warning("콴텍 리밸런싱 미실행 월 %s (user=%s) — 추천만 나가고 "
                        "실행되지 않았다", ", ".join(stuck), uid)

    # v3.46 — catch-up: 첫 영업일 당일이면 09:30 이후, 그 이후 날짜면 켜진 즉시 발화.
    # (기존엔 첫 영업일 정시 tick에만 발화 → 재시작 시 영영 안 떠 콴텍 슬롯이 멈췄음)
    if not _asch.monthly_rebalance_due(
        now,
        already_pushed=False,
        is_first_biz=is_first_business_day(now),
        first_biz_passed=_first_business_day_passed(now),
        hour=REBALANCE_HOUR,
        minute=REBALANCE_MINUTE,
    ):
        return

    log.info(f"💼 콴텍봇 월간 리밸런싱 푸시 — {month_key}, pending {len(pending)}명")

    # 무거운 호출이라 한 번만 실행해 모든 user에 같은 결과 푸시
    try:
        text, _ = await asyncio.to_thread(quant_run, "recommend")
    except Exception as e:
        log.exception("quant_run 실패")
        for uid in pending:
            try:
                await ctx.bot.send_message(
                    chat_id=int(uid),
                    text=f"⚠️ 월간 리밸런싱 추천 실패: {e}",
                )
            except Exception:
                pass
        return

    header = (
        f"💼 콴텍봇 월간 리밸런싱 — {month_key} 첫 영업일\n"
        f"(슬롯 40% 운용 · plan §5 분산 슬롯 비율)\n\n"
    )

    # 구조화된 추천 목록도 함께 가져옴 (paper 실행용) — v3.29
    try:
        snap = await asyncio.to_thread(quant_snapshot)
        phase = snap.consensus_phase if snap else None
        if phase:  # v3.45 — 월별 국면 실측 캐시 축적(상관 분석용)
            try:
                import trade_analytics as _ta

                _ta.record_phase_month(phase)
            except Exception:
                pass
        recs = await asyncio.to_thread(quant_recommend, phase) if phase else []
    except Exception:
        recs = []

    # ── diff 계산 (기존 포지션 vs 새 추천) ─────────────────────────────
    try:
        _all_slots = _pdb.list_slots()
        _qslot = next((s for s in _all_slots if "콴텍" in s.get("name", "")), None)
        _qpos = _pdb.list_positions(slot=int(_qslot["id"])) if _qslot else []
    except Exception:
        _qpos = []

    # v3.46 fix: StockRecommendation(객체)/dict 모두 안전 처리. 기존 getattr 기본값이
    # r.get을 항상 평가해 객체일 때 AttributeError로 catch-up 잡이 크래시했음.
    _rec_tickers = (
        {(r.ticker if hasattr(r, "ticker") else r.get("ticker", "")) for r in recs}
        if recs
        else set()
    )
    _held_tickers = {p["ticker"] for p in _qpos if p.get("quantity", 0) > 0}
    _q_keep = _held_tickers & _rec_tickers
    _q_exit = _held_tickers - _rec_tickers
    _q_enter = _rec_tickers - _held_tickers
    diff_line = (
        f"\n\n📋 포트폴리오 변경 ({month_key})\n"
        f"♻️ 유지 {len(_q_keep)} | 📤 청산 {len(_q_exit)} | 🆕 신규 {len(_q_enter)}"
    )

    # v3.46 — 성과 기반 비중 제안 첨부(추천 전용, 자동 적용 안 함). 표본 얇으면 중립 문구.
    try:
        _fb = _sfeedback.proposal()
    except Exception as e:
        log.warning(f"비중 제안 생성 실패 — 생략: {e}")
        _fb = ""
    fb_block = ("\n\n" + _fb) if _fb else ""

    for uid in pending:
        try:
            full = header + text + diff_line + fb_block
            parts = split_for_telegram(full)
            for part in parts[:-1]:
                await ctx.bot.send_message(
                    chat_id=int(uid),
                    text=part,
                    disable_web_page_preview=True,
                )
            # 마지막 파트에 승인 버튼 부착
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ 실행 (청산 {len(_q_exit)} / 신규 {len(_q_enter)})",
                            callback_data=f"quant_paper:approve:{uid}:{month_key}",
                        ),
                        InlineKeyboardButton(
                            "❌ 이번 달 건너뜀",
                            callback_data=f"quant_paper:skip:{uid}:{month_key}",
                        ),
                    ]
                ]
            )
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=parts[-1] + "\n\n📌 콴텍봇 슬롯 paper 실행하시겠습니까?",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            _PENDING_REBALANCE[uid] = recs
            _PENDING_REBALANCE_MONTH[uid] = month_key
            # 재시작에도 살아남게 디스크에 남긴다(v3.59)
            _rp.save_pending(REBALANCE_PENDING_FILE, uid, month_key, recs)
            _rp.mark_pushed(flag, month_key, uid, now.date())
            log.info(
                f"  → user_id={uid} 발송 완료 (청산 {len(_q_exit)} / 신규 {len(_q_enter)})"
            )
        except Exception as e:
            log.error(f"리밸런싱 발송 실패 user_id={uid}: {e}")

    # (푸시 기록은 위에서 uid별로 mark_pushed 했다 — v3.59)
    # 오래된 키 정리 (12개월 초과)
    if len(flag) > 12:
        for k in sorted(flag.keys())[:-12]:
            flag.pop(k, None)
    _save_rebalance_flag(flag)


# ─── 키움봇 주간 스캔 잡 + paper 승인 콜백 (v3.29) ──────────────────────────


def _load_kium_flag() -> dict:
    """주간 중복 푸시 방지 플래그.

    2026-08-31: 모듈 레벨 `json` import가 없어 여기서 NameError가 났는데,
    `except Exception`이 그것을 삼켜 **항상 빈 dict를 돌려주고 있었다.**
    저장도 같은 이유로 실패했다(그쪽은 try가 없어 로그에 드러났다).
    플래그가 늘 비면 "이번 주 이미 처리했는가"를 판정할 수 없어 6시간마다
    같은 주에 다시 푸시·매수할 수 있다.

    파일 오류만 조용히 넘긴다 — 넓은 except는 코딩 오류를 '파일 없음'으로
    위장시킨다. 이 프로젝트에서 `.env` 로딩 때 똑같이 겪었다.
    """
    try:
        return json.loads(KIUM_FLAG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_kium_flag(data: dict) -> None:
    KIUM_FLAG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def kium_weekly_scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """6h 주기 호출. 매주 월요일(weekday=0) 09:05 이후만 스캔 + 텔레그램 푸시."""
    from datetime import datetime as _dt

    now = _dt.now()
    if now.weekday() != 0:
        return
    if now.hour < KIUM_SCAN_HOUR or (
        now.hour == KIUM_SCAN_HOUR and now.minute < KIUM_SCAN_MINUTE
    ):
        return

    week_key = now.strftime("%Y-W%W")
    flag = _load_kium_flag()
    pushed = set(flag.get(week_key, []))
    pending = [uid for uid in ALLOWED_IDS if uid not in pushed]
    if not pending:
        return

    log.info(f"📊 키움봇 주간 스캔 푸시 — {week_key}, pending {len(pending)}명")

    try:
        results = await asyncio.to_thread(kium_scan, market="KOSPI200", top_n=8)
    except Exception as e:
        log.exception("kium_scan 실패")
        for uid in pending:
            try:
                await ctx.bot.send_message(
                    chat_id=int(uid),
                    text=f"⚠️ 키움봇 주간 스캔 실패: {e}",
                )
            except Exception:
                pass
        return

    if not results:
        log.warning("키움봇 스캔 결과 없음")
        return

    # ── diff 계산 (기존 포지션 vs 새 추천) ─────────────────────────────
    try:
        _all_slots = _pdb.list_slots()
        _kslot = next((s for s in _all_slots if "키움" in s.get("name", "")), None)
        _existing_pos = _pdb.list_positions(slot=int(_kslot["id"])) if _kslot else []
    except Exception:
        _existing_pos = []

    existing_tickers = {p["ticker"] for p in _existing_pos if p.get("quantity", 0) > 0}
    new_tickers = {r["ticker"] for r in results}
    keep_tickers = existing_tickers & new_tickers
    exit_tickers = existing_tickers - new_tickers
    enter_tickers = new_tickers - existing_tickers
    n_keep, n_exit, n_enter = len(keep_tickers), len(exit_tickers), len(enter_tickers)

    # 변경 없으면 알림 스킵 (이미 최적 포트폴리오 유지 중)
    if not exit_tickers and not enter_tickers:
        log.info(f"키움봇 {week_key}: 변경 없음 ({n_keep}종목 유지) — 알림 스킵")
        flag[week_key] = sorted(pushed | set(pending))
        _save_kium_flag(flag)
        return

    # ── 메시지 구성 ─────────────────────────────────────────────────
    lines = [f"📊 키움봇 주간 리밸런싱 — {week_key}\n"]
    if keep_tickers:
        keep_names = [r["name"] for r in results if r["ticker"] in keep_tickers]
        lines.append(f"♻️ 유지 {n_keep}종목: {', '.join(keep_names)}")
    if exit_tickers:
        exit_names = [
            p.get("name", t)
            for p in _existing_pos
            for t in [p["ticker"]]
            if t in exit_tickers
        ]
        lines.append(f"📤 청산 {n_exit}종목: {', '.join(exit_names)}")
    if enter_tickers:
        lines.append(f"\n🆕 신규 매수 {n_enter}종목:")
        for i, r in enumerate([x for x in results if x["ticker"] in enter_tickers], 1):
            price_str = (
                f"{r.get('current_price', 0):,.0f}원" if r.get("current_price") else "-"
            )
            lines.append(
                f"  {i}. {r['name']}({r['ticker']})"
                f"  점수:{r.get('score', 0):.2f}  {price_str}"
            )
    lines.append(f"\n→ 승인 시: 청산 {n_exit}종목 + 신규 {n_enter}종목 매수")
    scan_text = "\n".join(lines)

    for uid in pending:
        try:
            kb_uid = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ 실행 (청산 {n_exit} / 신규 {n_enter})",
                            callback_data=f"kium_paper:approve:{uid}:{week_key}",
                        ),
                        InlineKeyboardButton(
                            "❌ 이번 주 건너뜀",
                            callback_data=f"kium_paper:skip:{uid}:{week_key}",
                        ),
                    ]
                ]
            )
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=scan_text + "\n\n📌 키움봇 슬롯 paper 실행하시겠습니까?",
                reply_markup=kb_uid,
                disable_web_page_preview=True,
            )
            _PENDING_KIUM[uid] = results
            _PENDING_KIUM_WEEK[uid] = week_key
            pushed.add(uid)
            log.info(f"  → user_id={uid} 발송 완료 (청산 {n_exit} / 신규 {n_enter})")
        except Exception as e:
            log.error(f"키움봇 발송 실패 user_id={uid}: {e}")

    flag[week_key] = sorted(pushed)
    if len(flag) > 52:
        for k in sorted(flag.keys())[:-52]:
            flag.pop(k, None)
    _save_kium_flag(flag)


async def handle_kium_paper_callback(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """키움봇 InlineKeyboard 승인/거부 -> paper_db 매매 실행."""
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("kium_paper:"):
        return
    parts = query.data.split(":")
    if len(parts) < 4:
        return
    cb_action = parts[1]
    uid = int(parts[2])
    week_key = parts[3]

    if update.effective_user.id != uid:
        await query.message.reply_text("다른 사용자의 버튼입니다.")
        return

    if cb_action == "skip":
        _PENDING_KIUM.pop(uid, None)
        _PENDING_KIUM_WEEK.pop(uid, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⏭ {week_key} 키움봇 스캔 건너뜁니다.")
        log.info(f"키움봇 paper 거부 — user={uid}, week={week_key}")
        return

    results = _PENDING_KIUM.pop(uid, [])
    _PENDING_KIUM_WEEK.pop(uid, None)
    await query.edit_message_reply_markup(reply_markup=None)

    if not results:
        await query.message.reply_text(
            "저장된 스캔 결과가 없습니다. 봇이 재시작됐거나 이미 처리됐을 수 있습니다."
        )
        return

    try:
        slots = _list_runtime_paper_slots()
        kium_slot = next((s for s in slots if "키움" in s.get("name", "")), None)
        if not kium_slot:
            kium_slot = slots[1] if len(slots) > 1 else slots[0]
        slot_id = kium_slot["id"]
        slot_cap = kium_slot.get("current_capital", 0)
        slot_name = kium_slot.get("name", "키움")
    except Exception as e:
        await query.message.reply_text(f"슬롯 조회 실패: {e}")
        return

    await query.message.reply_text(
        f"⏳ {week_key} 키움봇 실행 중... ({len(results)}종목, 자본 {slot_cap:,.0f}원)"
    )
    lines_result = []

    # 기존 손절 기준 초과 포지션 정리 (-7% 하드스탑)
    STOP_LOSS_PCT = -7.0
    try:
        existing = _list_runtime_paper_positions(slot=int(slot_id))
    except Exception:
        existing = []

    kium_tickers = {r["ticker"] for r in results}
    for position_index, pos in enumerate(existing):
        avg = pos.get("avg_price", 0)
        qty = pos.get("quantity", 0)
        ticker = pos.get("ticker", "")
        if avg <= 0 or qty <= 0:
            continue
        # 현재가 조회
        cur_price = (
            next(
                (r.get("current_price", 0) for r in results if r["ticker"] == ticker), 0
            )
            or avg
        )
        pnl_pct = (cur_price - avg) / avg * 100 if avg > 0 else 0
        should_sell = (pnl_pct <= STOP_LOSS_PCT) or (ticker not in kium_tickers)
        if should_sell:
            try:
                notes = f"[AUTO] {week_key} 키움봇 청산 (pnl {pnl_pct:.1f}%)"
                if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                    _direct_paper_write(
                        _pdb.record_sell,
                        int(slot_id),
                        ticker,
                        qty,
                        cur_price,
                        notes=notes,
                    )
                else:
                    await _execute_private_paper_write(
                        caller="telegram-kium",
                        action="sell",
                        slot_id=int(slot_id),
                        actor_id=uid,
                        source_event_id=update.update_id,
                        item_key=f"sell-{position_index}",
                        ticker=ticker,
                        quantity=int(qty),
                        price=float(cur_price),
                        notes=notes,
                        user_approved=True,
                        policy_approved=False,
                    )
                reason = f"손절({pnl_pct:.1f}%)" if pnl_pct <= STOP_LOSS_PCT else "교체"
                lines_result.append(
                    f"  📤 {pos.get('name', ticker)} {qty}주 @{cur_price:,.0f}원 매도 [{reason}]"
                )
            except Exception as ex:
                lines_result.append(f"  ⚠ {ticker} 매도 실패: {ex}")

    # 신규 종목 균등 매수 (이미 보유 중이면 건너뜀)
    existing_tickers = {p["ticker"] for p in existing if p.get("quantity", 0) > 0}
    new_results = [r for r in results if r["ticker"] not in existing_tickers]
    # v3.59 — **목표 주식 비중을 실제 예산으로 옮긴다.**
    # 예전: `slot_cap / len(results)` — 분자는 현금만, 분모는 보유분 포함.
    # 목표라는 개념이 없어 보유/현금 구성에 따라 결과가 71~100%를 떠다녔다
    # (2026-08-31 실측: 이번 배치는 '의도 73.1%'였는데 정수 내림으로 66.7%).
    alloc_per, _budget = _equity_budget(
        slot_cap, existing, new_results, source=f"{week_key} 키움봇")

    hard_stop = _slot_hard_stop_reason(slot_name)
    if hard_stop:
        new_results = []          # 매도·교체는 이미 위에서 끝났고, 신규 진입만 막는다
        lines_result.append(f"  🛑 {hard_stop}")
    _over = _equity_block_line(_budget)
    if _over:
        new_results = []          # 강제 매도는 하지 않는다 — 신규 진입만 막는다
        lines_result.append(f"  {_over}")

    # v3.49: 진입 근거 태그 — 스캔 순위는 필터 전 원본 목록 기준이라야 의미가 있다
    _rank_of = {r.get("ticker"): i + 1 for i, r in enumerate(results)}


    # v3.51: 진입가 스테일 관문. 한 배치의 여러 종목이 같은 과거 날짜 종가와 원 단위까지
    # 일치하면 오래된 가격이다(2026-06-08 사고: 8종목이 4거래일 전 종가와 일치 → −678만원).
    # 틀린 진입가로 사면 손절·성과·회고가 전부 오염되므로 매수만 막는다. 매도는 막지 않는다.
    if new_results:
        try:
            import price_sanity as _ps

            _v = await asyncio.to_thread(
                _ps.check_batch,
                [{"ticker": r["ticker"], "name": r["name"],
                  "price": r.get("current_price", 0)} for r in new_results],
                _now_kst().strftime("%Y%m%d"))
            if _v["stale"]:
                lines_result.append(_ps.format_block(_v))
                new_results = []
            elif _v.get("warnings"):
                lines_result.append(_ps.format_warnings(_v))
        except Exception:
            log.warning("진입가 점검 실패 — 통과시킴", exc_info=True)

    # v3.50: 장외 신호는 체결하지 않고 큐에 넣는다.
    # 21시의 "현재가"는 당일 종가라 실전에서는 그 가격에 살 수 없다.
    import pending_orders as _po

    _now_kst_ = _now_kst()
    if new_results and not _po.is_market_hours(_now_kst_):
        _queued = []
        for _r in new_results:
            _px = _r.get("current_price", 0)
            if not _px or _px <= 0:
                continue
            try:
                import entry_tags as _et
                _tags = _et.kium_tags(_r, _rank_of.get(_r["ticker"]))
            except Exception:
                _tags = []
            _queued.append(_po.make_order(
                slot=slot_name, ticker=_r["ticker"], name=_r["name"],
                signal_price=float(_px), alloc=float(alloc_per),
                signal_at=_now_kst_, source=f"{week_key} 키움봇", tags=_tags))
        if _queued:
            await asyncio.to_thread(_po.enqueue, _queued)
            lines_result.append(_po.format_queued(_queued))
            new_results = []

    # v3.53: 체결가는 스캔가(일봉 종가)가 아니라 **실행 시점 실시세**로 확정한다.
    # 스캔 결과의 current_price는 일봉 마지막 종가라, 장중에 사도 전 거래일 종가가
    # 진입가로 남는다. 실시세를 못 얻으면 일봉으로 대체하지 않고 대기 큐로 보낸다.
    _resolved: dict = {}
    if new_results:
        import execution_price as _ep

        _live = await asyncio.gather(
            *(asyncio.to_thread(_get_execution_price, r["ticker"]) for r in new_results)
        )
        _res = _ep.resolve_batch(
            [{"ticker": r["ticker"], "name": r["name"], "price": r.get("current_price", 0)}
             for r in new_results],
            {r["ticker"]: px for r, px in zip(new_results, _live)}, alloc_per)
        _note = _ep.format_resolution(_res)
        if _note:
            lines_result.append(_note)
        _defer = [r for r in _res if r["verdict"] == "defer"]
        if _defer:
            _queued = []
            for _d in _defer:
                _src = next(r for r in new_results if r["ticker"] == _d["ticker"])
                try:
                    import entry_tags as _et
                    _tags = _et.kium_tags(_src, _rank_of.get(_d["ticker"]))
                except Exception:
                    _tags = []
                _queued.append(_po.make_order(
                    slot=slot_name, ticker=_d["ticker"], name=_d["name"],
                    signal_price=float(_d["signal_price"]), alloc=float(alloc_per),
                    signal_at=_now_kst_, source=f"{week_key} 키움봇", tags=_tags))
            if _queued:
                await asyncio.to_thread(_po.enqueue, _queued)
        _resolved = {r["ticker"]: r for r in _res if r["verdict"] == "fill"}
        # 체결가 자체가 오래된 종가면 여기서 막는다(스캔가 관문과 별개다 —
        # 손실을 만드는 것은 기록되는 가격이다).
        _fill_stale = _fill_price_stale_block(_resolved)
        if _fill_stale:
            lines_result.append(_fill_stale)
            _resolved = {}
        new_results = [r for r in new_results if r["ticker"] in _resolved]
        _apply_second_pass(_resolved, alloc_per, _budget,
                           source=f"{week_key} 키움봇")

    for result_index, r in enumerate(new_results):
        _fill = _resolved[r["ticker"]]
        price, qty = _fill["price"], _fill["qty"]
        try:
            notes = f"[AUTO] {week_key} 키움봇"
            try:  # 태그는 부가 정보다 — 실패해도 매수를 막지 않는다
                import entry_tags

                notes = entry_tags.format_note(
                    notes, entry_tags.kium_tags(r, _rank_of.get(r["ticker"]))
                )
            except Exception:
                log.warning("키움 진입 태그 생성 실패", exc_info=True)
            if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                _direct_paper_write(
                    _pdb.record_buy,
                    int(slot_id),
                    r["ticker"],
                    r["name"],
                    qty,
                    price,
                    notes=notes,
                )
            else:
                await _execute_private_paper_write(
                    caller="telegram-kium",
                    action="buy",
                    slot_id=int(slot_id),
                    actor_id=uid,
                    source_event_id=update.update_id,
                    item_key=f"buy-{result_index}",
                    ticker=r["ticker"],
                    name=r["name"],
                    quantity=qty,
                    price=float(price),
                    notes=notes,
                    user_approved=True,
                    policy_approved=False,
                )
            lines_result.append(
                f"  📥 {r['name']}({r['ticker']}) {qty}주 @{price:,.0f}원 = {qty*price:,.0f}원"
            )
        except Exception as ex:
            lines_result.append(f"  ⚠ {r['name']} 매수 실패: {ex}")

    sep = "\n"
    result_msg = (
        f"✅ {week_key} 키움봇 paper 완료\n"
        f"슬롯: {slot_name} ({slot_cap:,.0f}원 / {len(results)}종목)\n\n"
        + sep.join(lines_result)
        + "\n\n📊 http://localhost:8080 → 📊 키움봇 신호 탭"
    )
    for part in split_for_telegram(result_msg):
        await query.message.reply_text(part, disable_web_page_preview=True)
    log.info(f"키움봇 paper 완료 — {len(lines_result)}건, week={week_key}")


# ─── 콴텍봇 paper 매매 승인 콜백 (v3.29) ────────────────────────────────────


async def handle_quant_paper_callback(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """InlineKeyboard 승인/거부 -> paper_db 매매 실행."""
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("quant_paper:"):
        return
    parts = query.data.split(":")
    if len(parts) < 4:
        return
    cb_action = parts[1]
    uid = int(parts[2])
    month_key = parts[3]

    if update.effective_user.id != uid:
        await query.message.reply_text("다른 사용자의 버튼입니다.")
        return

    import rebalance_pending as _rp

    def _record_decision() -> None:
        """실행·건너뜀을 플래그에 남긴다. **둘 다 '사람이 판단했다'이므로
        다시 조르지 않는다.** 이 기록이 없어서 2026-07이 통째로 넘어갔다."""
        try:
            flag = _load_rebalance_flag()
            _rp.mark_executed(flag, month_key, uid)
            _save_rebalance_flag(flag)
        except Exception:
            log.warning("리밸런싱 실행 기록 실패", exc_info=True)

    if cb_action == "skip":
        _PENDING_REBALANCE.pop(uid, None)
        _PENDING_REBALANCE_MONTH.pop(uid, None)
        _rp.clear_pending(REBALANCE_PENDING_FILE)
        _record_decision()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⏭ {month_key} 리밸런싱 건너닙니다.")
        log.info(f"콴텍봇 paper 리밸런싱 거부 — user={uid}, month={month_key}")
        return

    recs = _PENDING_REBALANCE.pop(uid, [])
    _PENDING_REBALANCE_MONTH.pop(uid, None)
    if not recs:
        # v3.59 — 메모리에 없으면 디스크에서 되살린다. 봇 재시작으로 추천이
        # 사라져 그 달이 통째로 넘어가는 일이 2026-07에 실제로 있었다.
        recs = _rp.load_pending(REBALANCE_PENDING_FILE, uid, month_key)
        if recs:
            log.info("콴텍 승인 대기 %d건을 디스크에서 복원 — user=%s, month=%s",
                     len(recs), uid, month_key)
    await query.edit_message_reply_markup(reply_markup=None)

    if not recs:
        await query.message.reply_text(
            f"저장된 {month_key} 추천 종목이 없습니다. "
            "봇 재시작 전에 발송된 알림이거나 이미 처리된 달입니다.\n"
            "`/콴텍 추천`으로 다시 받으실 수 있습니다."
        )
        return

    try:
        slots = _list_runtime_paper_slots()
        quant_slot = next((s for s in slots if "콴텍" in s.get("name", "")), slots[0])
        slot_id = quant_slot["id"]
        slot_cap = quant_slot.get("current_capital", 0)
        slot_name = quant_slot.get("name", "콴텍")
    except Exception as e:
        await query.message.reply_text(f"슬롯 조회 실패: {e}")
        return

    n_recs = len(recs)
    await query.message.reply_text(f"⏳ {month_key} 리밸런싱 실행 중... ({n_recs}종목)")

    lines_result = []
    try:
        existing = _list_runtime_paper_positions(slot=int(slot_id))
    except Exception:
        existing = []

    # ── diff 계산 — 퇴출 종목만 청산, 유지 종목은 그대로 ────────────────────
    def _rec_attr(rec, key, default=None):
        return getattr(rec, key, None) if hasattr(rec, key) else rec.get(key, default)

    rec_tickers = {_rec_attr(r, "ticker", "") for r in recs}
    held = {p["ticker"]: p for p in existing if p.get("quantity", 0) > 0}

    # 1) 퇴출 청산: 현재 보유 중이지만 새 추천에 없는 종목
    for position_index, (ticker, pos) in enumerate(held.items()):
        if ticker in rec_tickers:
            continue  # 유지 → 손대지 않음
        try:
            avg = float(pos.get("avg_price", 0))
            qty = int(pos.get("quantity", 0))
            if qty <= 0 or avg <= 0:
                continue
            # 청산은 폴백을 허용한다(가격을 모르면 정리 자체를 못 함).
            # 다만 **평균단가로 채우지 않는다** — 그러면 손익 0%인 가짜 청산이 기록된다.
            cur = await asyncio.to_thread(_get_realtime_price, ticker)
            if not cur or cur <= 0:
                lines_result.append(
                    f"  ⚠ {pos.get('name', ticker)}: 시세를 얻지 못해 퇴출청산 보류 "
                    f"— 장중 모니터가 다시 판정합니다")
                continue
            notes = f"[AUTO] {month_key} 콴텍봇 퇴출청산"
            if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                _direct_paper_write(
                    _pdb.record_sell,
                    int(slot_id),
                    ticker,
                    qty,
                    cur,
                    notes=notes,
                )
            else:
                await _execute_private_paper_write(
                    caller="telegram-quant",
                    action="sell",
                    slot_id=int(slot_id),
                    actor_id=uid,
                    source_event_id=update.update_id,
                    item_key=f"sell-{position_index}",
                    ticker=ticker,
                    quantity=qty,
                    price=float(cur),
                    notes=notes,
                    user_approved=True,
                    policy_approved=False,
                )
            pname = pos.get("name", ticker)
            pnl_pct = (cur - avg) / avg * 100 if avg > 0 else 0
            lines_result.append(
                f"  📤 {pname} {qty}주 @{cur:,.0f}원 매도 (avg {avg:,.0f} / pnl {pnl_pct:+.1f}%)"
            )
        except Exception as ex:
            lines_result.append(f"  ⚠ {pos.get('ticker','?')} 매도 실패: {ex}")

    # 2) 유지 종목 현황 표시
    for ticker in rec_tickers & held.keys():
        pos = held[ticker]
        avg = float(pos.get("avg_price", 0))
        qty = int(pos.get("quantity", 0))
        lines_result.append(
            f"  ♻️ {pos.get('name', ticker)} {qty}주 유지 (평균가 {avg:,.0f}원)"
        )

    # 3) 신규 매수: 새 추천에 있지만 보유 안 한 것만
    new_recs = [r for r in recs if _rec_attr(r, "ticker", "") not in held]

    # v3.49: 진입 근거 태그 — 국면은 스캔 때 기록해 둔 캐시에서 읽는다(추가 조회 없음)
    _entry_phase = None
    try:
        import trade_analytics as _ta_phase

        _entry_phase = _ta_phase.load_phase_months().get(month_key)
    except Exception:
        log.warning("국면 캐시 조회 실패", exc_info=True)

    hard_stop = _slot_hard_stop_reason(slot_name)
    if hard_stop:
        new_recs = []             # 퇴출 청산은 이미 끝났고, 신규 진입만 막는다
        lines_result.append(f"  🛑 {hard_stop}")
    if new_recs:
        # 청산 후 슬롯 자본 재조회
        try:
            _refreshed = _runtime_paper_slot_summary(int(slot_id))
            available_cap = (
                float(_refreshed.get("current_capital", slot_cap))
                if _refreshed
                else slot_cap
            )
        except Exception:
            available_cap = slot_cap
        alloc_per, _budget = _equity_budget(
            available_cap, existing, new_recs, source=f"{month_key} 콴텍봇")
        _over = _equity_block_line(_budget)
        if _over:
            new_recs = []         # 강제 매도는 하지 않는다 — 신규 진입만 막는다
            lines_result.append(f"  {_over}")

        # v3.51: 진입가 스테일 관문 (키움 경로와 같은 이유)
        if new_recs:
            try:
                import price_sanity as _ps

                _v = await asyncio.to_thread(
                    _ps.check_batch,
                    [{"ticker": _rec_attr(r, "ticker", ""), "name": _rec_attr(r, "name", "?"),
                      "price": _rec_attr(r, "current_price", 0) or 0} for r in new_recs],
                    _now_kst().strftime("%Y%m%d"))
                if _v["stale"]:
                    lines_result.append(_ps.format_block(_v))
                    new_recs = []
                elif _v.get("warnings"):
                    lines_result.append(_ps.format_warnings(_v))
            except Exception:
                log.warning("진입가 점검 실패 — 통과시킴", exc_info=True)

        # v3.50: 장외 신호는 체결하지 않고 큐에 넣는다(키움 경로와 같은 이유)
        import pending_orders as _po

        _now_q = _now_kst()
        if not _po.is_market_hours(_now_q):
            _queued = []
            for _rec in new_recs:
                _px = _rec_attr(_rec, "current_price", 0) or 0
                if _px <= 0:
                    continue
                try:
                    import entry_tags as _et
                    _tags = _et.quant_tags(_rec, _entry_phase)
                except Exception:
                    _tags = []
                _queued.append(_po.make_order(
                    slot=slot_name, ticker=_rec_attr(_rec, "ticker", ""),
                    name=_rec_attr(_rec, "name", "?"), signal_price=float(_px),
                    alloc=float(alloc_per), signal_at=_now_q,
                    source=f"{month_key} 콴텍봇", tags=_tags))
            if _queued:
                await asyncio.to_thread(_po.enqueue, _queued)
                lines_result.append(_po.format_queued(_queued))
                new_recs = []

        # v3.53: 체결가는 실행 시점 실시세로 확정한다(키움 경로와 같은 이유)
        _resolved: dict = {}
        if new_recs:
            import execution_price as _ep

            _live = await asyncio.gather(
                *(asyncio.to_thread(_get_execution_price, _rec_attr(r, "ticker", ""))
                  for r in new_recs))
            _res = _ep.resolve_batch(
                [{"ticker": _rec_attr(r, "ticker", ""), "name": _rec_attr(r, "name", "?"),
                  "price": _rec_attr(r, "current_price", 0) or 0} for r in new_recs],
                {_rec_attr(r, "ticker", ""): px for r, px in zip(new_recs, _live)},
                alloc_per)
            _note = _ep.format_resolution(_res)
            if _note:
                lines_result.append(_note)
            _defer = [r for r in _res if r["verdict"] == "defer"]
            if _defer:
                _queued = []
                for _d in _defer:
                    _src = next(r for r in new_recs
                                if _rec_attr(r, "ticker", "") == _d["ticker"])
                    try:
                        import entry_tags as _et
                        _tags = _et.quant_tags(_src, _entry_phase)
                    except Exception:
                        _tags = []
                    _queued.append(_po.make_order(
                        slot=slot_name, ticker=_d["ticker"], name=_d["name"],
                        signal_price=float(_d["signal_price"]), alloc=float(alloc_per),
                        signal_at=_now_q, source=f"{month_key} 콴텍봇", tags=_tags))
                if _queued:
                    await asyncio.to_thread(_po.enqueue, _queued)
            _resolved = {r["ticker"]: r for r in _res if r["verdict"] == "fill"}
            _fill_stale = _fill_price_stale_block(_resolved)
            if _fill_stale:
                lines_result.append(_fill_stale)
                _resolved = {}
            new_recs = [r for r in new_recs
                        if _rec_attr(r, "ticker", "") in _resolved]
            _apply_second_pass(_resolved, alloc_per, _budget,
                               source=f"{month_key} 콴텍봇")

        for result_index, rec in enumerate(new_recs):
            try:
                name = _rec_attr(rec, "name", "?")
                tkr = _rec_attr(rec, "ticker", "")
                _fill = _resolved[tkr]
                price, qty = _fill["price"], _fill["qty"]
                notes = f"[AUTO] {month_key} 콴텍봇 신규"
                try:  # 태그는 부가 정보다 — 실패해도 매수를 막지 않는다
                    import entry_tags

                    notes = entry_tags.format_note(
                        notes, entry_tags.quant_tags(rec, _entry_phase)
                    )
                except Exception:
                    log.warning("콴텍 진입 태그 생성 실패", exc_info=True)
                if _PRIVATE_PAPER_WRITE_EXECUTOR is None:
                    _direct_paper_write(
                        _pdb.record_buy,
                        int(slot_id),
                        tkr,
                        name,
                        qty,
                        price,
                        notes=notes,
                    )
                else:
                    await _execute_private_paper_write(
                        caller="telegram-quant",
                        action="buy",
                        slot_id=int(slot_id),
                        actor_id=uid,
                        source_event_id=update.update_id,
                        item_key=f"buy-{result_index}",
                        ticker=tkr,
                        name=name,
                        quantity=qty,
                        price=float(price),
                        notes=notes,
                        user_approved=True,
                        policy_approved=False,
                    )
                lines_result.append(
                    f"  📥 {name}({tkr}) {qty}주 @{price:,.0f}원 = {qty*price:,.0f}원"
                )
            except Exception as ex:
                lines_result.append(
                    f"  ⚠ {_rec_attr(rec, 'name', '?')} 매수 실패: {ex}"
                )

    sep = "\n"
    result_msg = (
        f"✅ {month_key} 콴텍봇 paper 리밸런싱 완료\n"
        f"슬롯: {slot_name} ({slot_cap:,.0f}원 / {n_recs}종목)\n\n"
        + sep.join(lines_result)
        + "\n\n📊 http://localhost:8080 → 🌐 콴텍봇 탭"
    )
    for part in split_for_telegram(result_msg):
        await query.message.reply_text(part, disable_web_page_preview=True)
    _record_decision()
    _rp.clear_pending(REBALANCE_PENDING_FILE)
    log.info(f"콴텍봇 paper 리밸런싱 완료 — {len(lines_result)}건, month={month_key}")


# ─── IPO봇 주간 스캔 잡 + paper 승인 콜백 (v3.30) ───────────────────────────


def _load_ipo_flag() -> dict:
    try:
        import json as _json

        return _json.loads(IPO_FLAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ipo_flag(data: dict) -> None:
    import json as _json

    REBALANCE_FLAG_DIR.mkdir(parents=True, exist_ok=True)
    IPO_FLAG_FILE.write_text(
        _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def ipo_weekly_scan_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """6h 주기 호출. 매주 월요일 09:10 이후 IPO 스캔 → A등급↑ 종목만 paper 구독 알림."""
    from datetime import datetime as _dt

    now = _dt.now()
    if now.weekday() != 0:
        return
    if now.hour < IPO_SCAN_HOUR or (
        now.hour == IPO_SCAN_HOUR and now.minute < IPO_SCAN_MINUTE
    ):
        return

    week_key = now.strftime("%Y-W%W")
    flag = _load_ipo_flag()
    pushed = set(flag.get(week_key, []))
    pending = [uid for uid in ALLOWED_IDS if uid not in pushed]
    if not pending:
        return

    log.info(f"📋 IPO봇 주간 스캔 — {week_key}, pending {len(pending)}명")

    try:
        results = await asyncio.to_thread(ipo_scan, days_ahead=30, top_n=20)
    except Exception as e:
        log.exception("ipo_scan 실패")
        for uid in pending:
            try:
                await ctx.bot.send_message(
                    chat_id=int(uid), text=f"⚠️ IPO봇 스캔 실패: {e}"
                )
            except Exception:
                pass
        return

    # v3.55: **수집 0건과 'A등급 없음'을 구분한다.**
    # 둘 다 "알림 생략"으로 조용히 끝나던 탓에, 데이터 소스가 몇 달째 0건을
    # 돌려주고 있는데도 아무도 눈치채지 못했다. 후보가 없는 것은 정상이지만
    # 수집 자체가 안 되는 것은 고장이다.
    if not results:
        zero_weeks = sorted(set(flag.get("_zero_weeks", [])) | {week_key})
        flag["_zero_weeks"] = zero_weeks[-12:]
        streak = len(zero_weeks)
        log.warning("IPO봇: 일정 수집 0건 (연속 %d주) — 데이터 소스 점검 필요", streak)
        if streak >= 2:      # 한 주는 정말 일정이 없을 수 있다
            for uid in pending:
                try:
                    await ctx.bot.send_message(
                        chat_id=int(uid),
                        text=(f"⚠️ IPO봇: 공모 일정 수집이 {streak}주 연속 0건입니다.\n"
                              f"청약 일정이 정말 없을 수도 있지만, KIND·38커뮤니케이션 "
                              f"수집이 막혔을 가능성이 큽니다.\n"
                              f"점검: python3 scripts/ipo_bot.py scan"))
                except Exception:
                    pass
        for uid in pending:
            pushed.add(uid)
        flag[week_key] = sorted(pushed)
        _save_ipo_flag(flag)
        return

    flag.pop("_zero_weeks", None)       # 수집이 되면 연속 카운트를 끊는다

    # v3.56: **'등급 미달'과 '등급 산출 자체가 불가'를 구분한다.**
    # 수집 0건 경고를 붙이고 나서 실제로 돌려 보니, 수집은 되고 있었고 후보 10건이
    # 전부 [?] 산출불가였다(확정 요소 1/5 — 주관사만). 필터는 A등급 이상만
    # 통과시키므로 이 상태는 영원히 0건이 된다. 판단을 못 한 것을 "매력 없음"으로
    # 넘기면, 고장이 정상처럼 보인다.
    diag = ipo_diagnose_scan(results, IPO_MIN_GRADE)
    hot = diag["hot"]

    if diag["verdict"] == "spac_only":
        # 스팩만 있으면 판단할 것이 없다 — 채점 대상이 아니기 때문이다.
        flag.pop("_ungraded_weeks", None)
        log.info("IPO봇: 후보 %d건 전원 스팩 — 채점 대상 아님", diag["n"])
        for uid in pending:
            pushed.add(uid)
        flag[week_key] = sorted(pushed)
        _save_ipo_flag(flag)
        return

    if diag["verdict"] == "pre_stage_only":
        # 전원 수요예측 전 — 등급이 없는 것이 정상이다. 경고하지 않는다.
        flag.pop("_ungraded_weeks", None)
        log.info("IPO봇: 후보 %d건 전원 수요예측 전 — 등급 산출은 수요예측 후",
                 diag["n"])
        for uid in pending:
            pushed.add(uid)
        flag[week_key] = sorted(pushed)
        _save_ipo_flag(flag)
        return

    if diag["verdict"] == "all_ungraded":
        ung_weeks = sorted(set(flag.get("_ungraded_weeks", [])) | {week_key})
        flag["_ungraded_weeks"] = ung_weeks[-12:]
        streak = len(ung_weeks)
        gaps = ipo_missing_factors(results)
        log.warning("IPO봇: 후보 %d건 전원 등급 산출불가 (연속 %d주) — 미확보 요소: %s",
                    diag["n"], streak, ", ".join(gaps) or "?")
        if streak >= 2:
            for uid in pending:
                try:
                    await ctx.bot.send_message(
                        chat_id=int(uid),
                        text=(f"⚠️ IPO봇: 후보 {diag['n']}건이 {streak}주 연속 "
                              f"**전원 등급 산출불가**입니다.\n"
                              f"수집은 되고 있으나 채점 입력이 없어 판단을 못 하는 "
                              f"상태입니다 (매력 없음이 아닙니다).\n"
                              f"미확보 요소: {', '.join(gaps) or '확인 필요'}\n"
                              f"점검: python3 scripts/ipo_bot.py scan"))
                except Exception:
                    pass
        for uid in pending:
            pushed.add(uid)
        flag[week_key] = sorted(pushed)
        _save_ipo_flag(flag)
        return

    flag.pop("_ungraded_weeks", None)    # 등급이 나오면 연속 카운트를 끊는다

    if not hot:
        # v3.56: 사전등급(수요예측 전) 후보는 **알리되 자동 구독은 걸지 않는다.**
        # 요소 2개로 낸 등급이라 확정 등급과 같이 다루면 정보가 거의 없는 종목이
        # A++로 올라온다. 대신 "언제 다시 보면 되는지"(수요예측 일정)를 준다.
        if diag["verdict"] == "preview_only":
            lines = [f"📋 IPO봇 사전 후보 — {week_key}",
                     f"(수요예측 전 {len(diag['preview'])}종목 · 참고용)",
                     ""]
            for i, r in enumerate(diag["preview"], 1):
                lines.append(
                    f"{i}. [{r.get('grade','?')}(사전)] {r.get('corp_name','?')}  "
                    f"공모 {r.get('offer_amount') or '?'}억 / "
                    f"{r.get('underwriter') or '주관사?'}")
                lines.append(
                    f"   수요예측 {r.get('demand_start') or '?'}~{r.get('demand_end') or '?'}"
                    f" · 청약 {r.get('sub_start') or '?'}~{r.get('sub_end') or '?'}")
            lines.append("")
            lines.append("※ 경쟁률·확정가가 없어 판단 근거가 얇습니다. "
                         "수요예측 종료 후 확정등급으로 다시 올라옵니다.")
            text = "\n".join(lines)
            for uid in pending:
                try:
                    await ctx.bot.send_message(chat_id=int(uid), text=text,
                                               disable_web_page_preview=True)
                except Exception:
                    log.warning("IPO 사전 후보 발송 실패 uid=%s", uid, exc_info=True)
            log.info("IPO봇: 사전등급 후보 %d건 안내 (구독 없음)", len(diag["preview"]))
        else:
            log.info("IPO봇: 후보 %d건이나 A등급 이상 없음 (등급: %s) — 알림 생략",
                     diag["n"], ", ".join(diag["grades"]))
        for uid in pending:
            pushed.add(uid)
        flag[week_key] = sorted(pushed)
        _save_ipo_flag(flag)
        return

    _GRADE_EMOJI = {"A++": "🏆", "A+": "🥇", "A": "🥈", "B": "🥉", "C": "📉", "?": "❓"}
    lines = [f"📋 IPO봇 주간 스캔 — {week_key}\n(A등급 이상 {len(hot)}종목)\n"]
    for i, r in enumerate(hot, 1):
        grade = r.get("grade", "?")
        score = r.get("total_score", 0) or 0
        name = r.get("corp_name", "?")
        sub_start = r.get("sub_start", "-")
        sub_end = r.get("sub_end", "-")
        listing = r.get("listing_date", "-")
        lines.append(
            f"{i}. {_GRADE_EMOJI.get(grade,'❓')} [{grade}] {name}  점수:{score:.0f}\n"
            f"   청약:{sub_start}~{sub_end}  상장:{listing}"
        )
    scan_text = "\n".join(lines)

    for uid in pending:
        try:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ paper 구독 신청 ({len(hot)}종목)",
                            callback_data=f"ipo_paper:approve:{uid}:{week_key}",
                        ),
                        InlineKeyboardButton(
                            "❌ 건너뜀",
                            callback_data=f"ipo_paper:skip:{uid}:{week_key}",
                        ),
                    ]
                ]
            )
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=scan_text + "\n\n📌 IPO 슬롯(20%)에 paper 구독 신청하시겠습니까?",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            _PENDING_IPO[uid] = hot
            pushed.add(uid)
            log.info(f"  → user_id={uid} IPO 알림 완료 ({len(hot)}종목)")
        except Exception as e:
            log.error(f"IPO 발송 실패 user_id={uid}: {e}")

    flag[week_key] = sorted(pushed)
    if len(flag) > 52:
        for k in sorted(flag.keys())[:-52]:
            flag.pop(k, None)
    _save_ipo_flag(flag)


async def handle_ipo_paper_callback(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """IPO InlineKeyboard 승인/거부 → paper_db ipo_upsert 기록."""
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("ipo_paper:"):
        return
    parts = query.data.split(":")
    if len(parts) < 4:
        return
    cb_action = parts[1]
    uid = int(parts[2])
    week_key = parts[3]

    if update.effective_user.id != uid:
        await query.message.reply_text("다른 사용자의 버튼입니다.")
        return

    if cb_action == "skip":
        _PENDING_IPO.pop(uid, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⏭ {week_key} IPO 구독 건너뜁니다.")
        log.info(f"IPO paper 거부 — user={uid}, week={week_key}")
        return

    hot = _PENDING_IPO.pop(uid, [])
    await query.edit_message_reply_markup(reply_markup=None)

    if not hot:
        await query.message.reply_text(
            "저장된 IPO 스캔 결과가 없습니다. 봇이 재시작됐거나 이미 처리됐습니다."
        )
        return

    # IPO 슬롯 자본금으로 균등 배분
    try:
        slots = _pdb.list_slots()
        ipo_slot = next(
            (
                s
                for s in slots
                if "IPO" in s.get("name", "") or "ipo" in s.get("name", "").lower()
            ),
            None,
        )
        if not ipo_slot and len(slots) >= 3:
            ipo_slot = slots[2]
        elif not ipo_slot:
            ipo_slot = slots[-1]
        slot_cap = ipo_slot.get("current_capital", 0) if ipo_slot else 0
    except Exception as e:
        await query.message.reply_text(f"슬롯 조회 실패: {e}")
        return

    alloc_per = slot_cap / len(hot) if hot else 0
    lines_result = []

    for r in hot:
        name = r.get("corp_name", "?")
        grade = r.get("grade", "?")
        score = r.get("total_score") or 0.0
        sub_start = r.get("sub_start")
        sub_end = r.get("sub_end")
        listing_date = r.get("listing_date")
        band_high = r.get("band_high") or r.get("offer_band_high")
        factors = {
            "demand_score": r.get("demand_score"),
            "band_score": r.get("band_score"),
            "float_score": r.get("float_score"),
            "underwriter_score": r.get("underwriter_score"),
            "offer_size_score": r.get("offer_size_score"),
            "band_high": band_high,
        }
        try:
            rec = _pdb.ipo_upsert(
                name=name,
                sub_start=sub_start,
                sub_end=sub_end,
                listing_date=listing_date,
                grade=grade,
                score=score,
                factors={k: v for k, v in factors.items() if v is not None},
                subscribed=True,
                alloc_amount=alloc_per,
            )
            lines_result.append(
                f"  📥 [{grade}] {name}  배정금액:{alloc_per:,.0f}원"
                f"  (상장:{listing_date or '-'})"
            )
            log.info(f"IPO upsert OK: {name} grade={grade} id={rec.get('id')}")
        except Exception as ex:
            lines_result.append(f"  ⚠ {name} 기록 실패: {ex}")
            log.error(f"IPO upsert 실패 {name}: {ex}")

    sep = "\n"
    result_msg = (
        f"✅ {week_key} IPO봇 paper 구독 완료\n"
        f"IPO 슬롯 자본 {slot_cap:,.0f}원 / {len(hot)}종목 균등\n\n"
        + sep.join(lines_result)
        + "\n\n📊 http://localhost:8080 → 🏷️ IPO봇 탭"
    )
    for part in split_for_telegram(result_msg):
        await query.message.reply_text(part, disable_web_page_preview=True)
    log.info(f"IPO paper 완료 — {len(lines_result)}건, week={week_key}")


async def cmd_test_ipo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_ipo — IPO봇 스캔 즉시 강제 실행 (요일·시간 체크 없이)."""
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        return
    await update.message.reply_text("⏳ IPO봇 스캔 중...")
    try:
        results = await asyncio.to_thread(ipo_scan, days_ahead=30, top_n=20)
    except Exception as e:
        await update.message.reply_text(f"❌ IPO봇 실패: {e}")
        log.exception("test_ipo 실패")
        return

    hot = [r for r in results if r.get("grade", "?") in IPO_MIN_GRADE]
    all_text = f"🧪 [테스트] IPO봇\n전체 {len(results)}종목 / A등급↑ {len(hot)}종목\n"
    if not results:
        await update.message.reply_text(all_text + "\n(결과 없음)")
        return

    _GRADE_EMOJI = {"A++": "🏆", "A+": "🥇", "A": "🥈", "B": "🥉", "C": "📉", "?": "❓"}
    lines = [all_text]
    for i, r in enumerate(results[:10], 1):
        grade = r.get("grade", "?")
        score = r.get("total_score", 0) or 0
        name = r.get("corp_name", "?")
        listing = r.get("listing_date", "-")
        lines.append(
            f"{i}. {_GRADE_EMOJI.get(grade,'❓')} [{grade}] {name}  점수:{score:.0f}  상장:{listing}"
        )
    scan_text = "\n".join(lines)

    if not hot:
        await update.message.reply_text(
            scan_text + "\n\n(A등급 이상 없음 — paper 구독 알림 안 함)"
        )
        return

    from datetime import datetime as _dt

    week_key = _dt.now().strftime("%Y-W%W") + "-test"
    _PENDING_IPO[uid] = hot

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ paper 구독 ({len(hot)}종목)",
                    callback_data=f"ipo_paper:approve:{uid}:{week_key}",
                ),
                InlineKeyboardButton(
                    "❌ 건너뜀", callback_data=f"ipo_paper:skip:{uid}:{week_key}"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        scan_text + f"\n\n📌 A등급↑ {len(hot)}종목 paper 구독?",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    log.info(f"/test_ipo — user={uid}, hot={len(hot)}")


# ─── 강제 실행 커맨드 (v3.30 개발용) ─────────────────────────────────────────


async def cmd_test_quant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_quant — 월간 리밸런싱 즉시 강제 실행 (요일·날짜 체크 없이)."""
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        return
    await update.message.reply_text("⏳ 콴텍봇 스캔 중... (수 분 소요)")
    try:
        text, _ = await asyncio.to_thread(quant_run, "recommend")
        snap = await asyncio.to_thread(quant_snapshot)
        phase = snap.consensus_phase if snap else None
        if phase:  # v3.45 — 월별 국면 실측 캐시 축적(상관 분석용)
            try:
                import trade_analytics as _ta

                _ta.record_phase_month(phase)
            except Exception:
                pass
        recs = await asyncio.to_thread(quant_recommend, phase) if phase else []
    except Exception as e:
        await update.message.reply_text(f"❌ 콴텍봇 실패: {e}")
        log.exception("test_quant 실패")
        return

    header = "🧪 [테스트] 콴텍봇 추천\n\n"
    _PENDING_REBALANCE[uid] = recs
    from datetime import datetime as _dt

    month_key = _dt.now().strftime("%Y-%m")
    _PENDING_REBALANCE_MONTH[uid] = month_key

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ paper 실행 ({len(recs)}종목 균등 매수)",
                    callback_data=f"quant_paper:approve:{uid}:{month_key}",
                ),
                InlineKeyboardButton(
                    "❌ 건너뜀", callback_data=f"quant_paper:skip:{uid}:{month_key}"
                ),
            ]
        ]
    )
    full = header + text
    parts = split_for_telegram(full)
    for part in parts[:-1]:
        await update.message.reply_text(part, disable_web_page_preview=True)
    await update.message.reply_text(
        parts[-1] + f"\n\n📌 paper 매매 실행? ({len(recs)}종목)",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    log.info(f"/test_quant — user={uid}, {len(recs)}종목 대기")


async def cmd_test_kium(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_kium — 주간 키움봇 스캔 즉시 강제 실행."""
    uid = update.effective_user.id
    if uid not in ALLOWED_IDS:
        return
    await update.message.reply_text("⏳ 키움봇 스캔 중... (수 분 소요)")
    try:
        results = await asyncio.to_thread(kium_scan, market="KOSPI200", top_n=8)
    except Exception as e:
        await update.message.reply_text(f"❌ 키움봇 실패: {e}")
        log.exception("test_kium 실패")
        return

    if not results:
        await update.message.reply_text("⚠️ 스캔 결과 없음 (장 마감/데이터 없음)")
        return

    from datetime import datetime as _dt

    week_key = _dt.now().strftime("%Y-W%W") + "-test"
    _PENDING_KIUM[uid] = results
    _PENDING_KIUM_WEEK[uid] = week_key

    lines = [f"🧪 [테스트] 키움봇 스캔\n"]
    for i, r in enumerate(results, 1):
        price_str = (
            f"{r.get('current_price', 0):,.0f}원" if r.get("current_price") else "-"
        )
        lines.append(
            f"{i}. {r['name']}({r['ticker']})"
            f"  점수:{r.get('score', 0):.2f}"
            f"  현재가:{price_str}"
        )
    scan_text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ paper 실행 ({len(results)}종목 균등 매수)",
                    callback_data=f"kium_paper:approve:{uid}:{week_key}",
                ),
                InlineKeyboardButton(
                    "❌ 건너뜀", callback_data=f"kium_paper:skip:{uid}:{week_key}"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        scan_text + "\n\n📌 paper 매매 실행?",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    log.info(f"/test_kium — user={uid}, {len(results)}종목 대기")


# ─── main ────────────────────────────────────────────


def main() -> None:
    _start_private_write_runtime()
    log.info(f"🤖 텔레그램 봇 시작 (허용 사용자: {len(ALLOWED_IDS)}명)")
    log.info(f"   마스터: {MASTER_MODEL} (라우팅)")
    log.info(f"   하위:   {LLM_MODEL} (지식봇)")
    log.info(f"   inbox:  {RAW_INBOX}")
    log.info(f"   메모리: chat_id 단위 in-memory (max {memory.max_turns}턴)")

    RAW_INBOX.mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("agent", cmd_agent))  # v3.42 범용 에이전트
    _setup_agent_tools()
    app.add_handler(CommandHandler("test_quant", cmd_test_quant))  # v3.30 강제 스캔
    app.add_handler(CommandHandler("test_kium", cmd_test_kium))  # v3.30 강제 스캔
    app.add_handler(CommandHandler("test_ipo", cmd_test_ipo))  # v3.30 강제 스캔
    # v3.30 IPO paper 승인 버튼
    app.add_handler(
        CallbackQueryHandler(handle_ipo_paper_callback, pattern=r"^ipo_paper:")
    )
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # v3.29 콴텍봇 paper 매매 승인 버튼
    app.add_handler(
        CallbackQueryHandler(handle_quant_paper_callback, pattern=r"^quant_paper:")
    )
    # v3.29 키움봇 paper 매매 승인 버튼
    app.add_handler(
        CallbackQueryHandler(handle_kium_paper_callback, pattern=r"^kium_paper:")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            ~filters.TEXT & ~filters.COMMAND & ~filters.Document.ALL, handle_other
        )
    )

    # 일정 알림 스케줄러 (v3.6) — JobQueue가 없으면 안전 스킵
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            notify_due_events,
            interval=NOTIFY_INTERVAL_SEC,
            first=10,  # 봇 시작 10초 후 첫 스캔
            name="schedule_notifier",
            job_kwargs={
                # 이전 실행이 아직 돌고 있으면 새 실행 건너뜀 (APScheduler 경고 방지)
                "max_instances": 1,
                # interval 내에 놓친 trigger는 하나로 합산 (중복 누적 방지)
                "coalesce": True,
                # 놓친 trigger는 최대 30초 이내까지만 즉시 실행, 초과 시 폐기
                "misfire_grace_time": 30,
            },
        )
        log.info(f"🔔 일정 알림 스케줄러 등록 (interval={NOTIFY_INTERVAL_SEC}s)")
        # v3.23 — 콴텍봇 월간 리밸런싱 (24h 주기 검사 → 첫 영업일 09:30 푸시 + 멱등)
        app.job_queue.run_repeating(
            quant_monthly_rebalance,
            interval=REBALANCE_CHECK_INTERVAL_SEC,
            first=60,  # 봇 시작 60초 후 첫 검사 (시동 안정성)
            name="quant_rebalance",
        )
        log.info(
            f"💼 콴텍봇 월간 리밸런싱 스케줄러 등록 "
            f"(매일 1회 검사 → 첫 영업일 {REBALANCE_HOUR:02d}:{REBALANCE_MINUTE:02d} 푸시)"
        )
        # v3.29 — 키움봇 주간 스캔 (6h 검사 → 매주 월 09:05 푸시)
        app.job_queue.run_repeating(
            kium_weekly_scan_job,
            interval=KIUM_CHECK_INTERVAL_SEC,
            first=90,
            name="kium_weekly_scan",
        )
        log.info("📊 키움봇 주간 스캔 스케줄러 등록 (6h 주기 → 매주 월 09:05 푸시)")
        # v3.30 — IPO봇 주간 스캔 (6h 검사 → 매주 월 09:10 A등급↑ 푸시)
        app.job_queue.run_repeating(
            ipo_weekly_scan_job,
            interval=IPO_CHECK_INTERVAL_SEC,
            first=120,
            name="ipo_weekly_scan",
        )
        log.info(
            "📋 IPO봇 주간 스캔 스케줄러 등록 (6h 주기 → 매주 월 09:10 A등급↑ 푸시)"
        )
        # v3.31/v3.44 — 장 중 실시간 손절·익절 모니터 (5분 간격, 장외 시간 자동 스킵)
        app.job_queue.run_repeating(
            intraday_monitor_job,
            interval=INTRADAY_MONITOR_INTERVAL_SEC,
            first=180,
            name="intraday_monitor",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )
        log.info(
            f"🔍 장 중 손절·익절 모니터 등록 ({INTRADAY_MONITOR_INTERVAL_SEC//60}분 간격 "
            "· 네이버 실시간+pykrx 폴백 · 평일 09:05~15:30 동작)"
        )
        # v3.60 — 목표 주식 비중 추종 점검 (콴텍·키움 · 상태가 바뀔 때만 알림)
        app.job_queue.run_repeating(
            equity_drift_job,
            interval=EQUITY_DRIFT_INTERVAL_SEC,
            first=420,
            name="equity_drift",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
        )
        log.info("⚖️ 목표 주식 비중 점검 등록 "
                 f"({EQUITY_DRIFT_INTERVAL_SEC // 3600}시간 간격 · "
                 f"{'·'.join(EQUITY_DRIFT_SLOTS)} · 강제 매도 없음)")

        # v3.57 — IPO 슬롯 유휴 자본 파킹 (평일 09:30~15:00)
        app.job_queue.run_repeating(
            idle_cash_job,
            interval=IDLE_CASH_INTERVAL_SEC,
            first=300,
            name="idle_cash",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 120},
        )
        log.info("💤 유휴 자본 파킹 등록 (IPO 슬롯 · 평일 09:30~15:00 · "
                 f"{idle_cash.PARK_NAME})")

        # v3.40 — 기술적 신호 봇 (1시간 간격 · 평일 09:00~15:30 · 워치리스트 1시간봉)
        app.job_queue.run_repeating(
            technical_signal_job,
            interval=SIGNAL_CHECK_INTERVAL_SEC,
            first=240,
            name="technical_signal",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )
        log.info("📡 기술적 신호 봇 등록 (1시간 간격 · 평일 09:00~15:30 · 멱등)")
        # v3.43 — 봇작업 예약 디스패처: 60초마다 due 검사 → 실제 봇 실행·전송
        _seed_default_schedules()
        app.job_queue.run_repeating(
            action_dispatch_job,
            interval=60,
            first=30,
            name="action_dispatch",
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 55},
        )
        log.info("⏰ 봇작업 예약 디스패처 등록 (60초 검사 → 자연어로 등록한 작업 실행)")

    else:
        log.warning(
            "⚠️ JobQueue 없음 — 일정 알림 비활성. PTB[job-queue] extra 설치 필요"
        )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
