#!/usr/bin/env python3
"""
마스터 라우터 — 사용자 입력 → 도구 선택 (JSON 출력 파싱).

Gemma 4 26B MoE (gemma4:26b)가 라우팅 전담:
- format=json + temperature=0 으로 결정론적 JSON 출력 유도
- 직전 대화 히스토리도 입력에 포함 → 멀티턴 의도 해석
- 시스템 프롬프트에 "오늘 날짜/시각" 동적 주입 → 자연어 일시 정확도 ↑
- 파싱 실패 시 안전하게 knowledge_bot fallback

현재 등록된 도구:
  knowledge_bot(query)        wiki RAG 검색 + Gemma 4 31B 답변
  schedule_bot(action, ...)   일정 등록/조회/삭제 (SQLite 백엔드)
  finance_bot(action, ...)    ECOS/FRED 지표 + Wiki 원칙 대조
  invest_bot(action, ...)     pykrx 차트 분석 + 매매 원칙 대조 (단일 종목)
  inbox_bot(content, hint)    명시적 메모 의도를 raw/inbox/에 자동 저장
  coding_bot(action, ...)     코드 설계/구현/디버깅/리뷰. 하이브리드 LLM
  respond_directly(answer)    LLM 출력 그대로 사용 (인사/잡담/메타)

모든 응답에 mode ∈ {"fast", "accurate"} 필수. fast는 LLM 호출 최소화,
accurate는 31B 풀 추론(또는 Claude API). 라우터가 입력의 정확도 요구를 자체 판단.
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime
from urllib.request import Request, urlopen

OLLAMA_URL = "http://127.0.0.1:11434"
MASTER_MODEL = "gemma4:26b"
KEEP_ALIVE = "30m"

log = logging.getLogger("router")


# ─── 결정론적 mode override (B-2, v3.13) ──────────────
# LLM이 사용자의 명시 mode 키워드를 무시할 경우의 안전망.
# 시스템 프롬프트의 Level 1 키워드와 동기화 — 둘 다 있어야 일관됨.
_ACCURATE_KEYWORDS_RE = re.compile(
    r"정확(?:히|하게)|자세(?:히|하게)|꼼꼼(?:히|하게)|제대로|"
    r"원칙대로|정밀(?:히|하게)|풀로|심층(?:적)?|근거(?:\s*들어)?|상세(?:히|하게)"
)
_FAST_KEYWORDS_RE = re.compile(
    r"빠르게|간단(?:히|하게)|짧게|간략(?:히|하게)|요약만|한\s*줄|대충|그냥(?:\s|$)"
)


def _override_mode_by_keywords(query: str, current_mode: str) -> str:
    """사용자 입력에 명시 mode 키워드가 있으면 LLM 판단 무시하고 강제.

    - 'accurate' 키워드만 있음 → "accurate"
    - 'fast' 키워드만 있음 → "fast"
    - 둘 다 있거나 둘 다 없으면 → LLM 판단(current_mode) 그대로
    """
    if not query:
        return current_mode
    has_acc = bool(_ACCURATE_KEYWORDS_RE.search(query))
    has_fast = bool(_FAST_KEYWORDS_RE.search(query))
    if has_acc and not has_fast:
        if current_mode != "accurate":
            log.info(f"🎯 mode override: {current_mode} → accurate (사용자 키워드)")
        return "accurate"
    if has_fast and not has_acc:
        if current_mode != "fast":
            log.info(f"🎯 mode override: {current_mode} → fast (사용자 키워드)")
        return "fast"
    return current_mode


# 등록된 도구 (라우터가 이 안에서만 선택)
KNOWN_TOOLS = {"knowledge_bot", "schedule_bot", "finance_bot", "invest_bot", "kium_bot", "quant_bot", "inbox_bot", "coding_bot", "ipo_bot", "news_bot", "action_schedule", "system_info", "respond_directly"}

# schedule_bot.action 허용값
SCHEDULE_ACTIONS = {"add", "list", "upcoming", "delete", "complete"}

# finance_bot.action 허용값
FINANCE_ACTIONS = {"latest", "dashboard", "compare_with_principles"}
# 라우터가 인지할 지표 키 (finance_bot.INDICATORS 와 동기화 필요)
FINANCE_INDICATORS = {"기준금리", "CPI", "USD/KRW", "FED", "DGS10", "VIX"}

# invest_bot.action 허용값
INVEST_ACTIONS = {"analyze", "compare_with_rules"}

# kium_bot.action 허용값 (v3.16 신규)
KIUM_ACTIONS = {"scan"}
KIUM_MARKETS = {"KOSPI200", "KOSDAQ150", "KOSPI200+KOSDAQ150"}

# ipo_bot.action 허용값 (v3.26 신규)
IPO_ACTIONS = {"scan", "analyze"}

# quant_bot.action 허용값 (v3.22 phase / v3.23 recommend 추가)
QUANT_ACTIONS = {"phase", "recommend"}
QUANT_MARKETS = {"KOSPI200", "KOSDAQ150", "KOSPI200+KOSDAQ150"}
QUANT_PHASES = {"Recovery", "Expansion", "Slowdown", "Contraction"}


# coding_bot.action 허용값
CODING_ACTIONS = {"design", "code", "debug", "review"}

# 모든 도구 출력에 포함되는 mode 필드 (속도/정확도 자동 판단)
VALID_MODES = {"fast", "accurate"}
DEFAULT_MODE = "accurate"


ROUTER_SYSTEM_PROMPT_TEMPLATE = """당신은 현준의 AI 비서 시스템의 마스터 라우터입니다.
사용자 입력을 분석해 가장 적절한 도구 하나를 호출하세요.

현재 시각(KST): {now_kst}
오늘 요일: {weekday_kor}

## 사용 가능한 도구

### 1. knowledge_bot(query: string)
- 목적: wiki/ 폴더의 정제된 노트(투자 원칙, 매매 규칙, 학습 메모, 콴텍·키움·IPO 봇 설계, 퀀트 팩터)에서 검색해 답변
- 사용 케이스: 사용자의 누적된 지식·원칙·메모와 관련된 모든 질문
- 예시: "내 매매 청산 규칙이 뭐야?", "콴텍봇 슬롯 비율은?", "모멘텀 크래시 신호?"
- 멀티턴: 직전 대화를 참조하는 질문도 knowledge_bot에 — "그거 더 자세히", "방금 그 노트 다시"
  → query를 명료하게 다시 작성: "그거 더 자세히" (직전: 매매 청산 규칙) → "내 매매 청산 규칙을 더 자세히 설명"

### 2. schedule_bot(action, ...)
- 목적: 개인 일정 등록·조회·삭제·완료 (SQLite 자체 관리, 자동 알림 푸시)
- action ∈ {{"add", "list", "upcoming", "delete", "complete"}}
- add 인자:
    title (필수, 짧은 명사구), when_at (필수, ISO 8601), notes (선택)
    rrule_freq (선택) ∈ {{"daily", "weekly", "monthly"}}  — 반복 일정
    rrule_byday (선택, weekly만 의미) "MO,WE,FR" 형식
    rrule_until (선택, ISO 8601) 반복 종료일
    pre_notify_minutes (선택, 정수 or 정수 list) — 사전 알림 분 수. 여러 알림 원하면 list (예: [30, 5])
    conflict_window_minutes (선택, 정수, 기본 15) — 0이면 충돌 검사 비활성. "그냥 등록"/"중복 OK"면 0
- list/upcoming → 인자 없음 / delete/complete → event_id
- when_at 규칙: ISO 8601 로컬 시각 "YYYY-MM-DDTHH:MM:SS". KST 기준. 자연어("내일 오후 3시", "다음주 수요일 10시")는 위의 현재 시각을 기준으로 직접 계산해서 ISO로 변환. 시각이 명시되지 않으면 09:00 가정.
- 예시:
    "다음주 화요일 오후 3시 콴텍봇 리뷰 잡아줘"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"콴텍봇 리뷰","when_at":"2026-05-12T15:00:00"}}}}
    "내일 오후 3시 회의 5분 전 알려줘"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"회의","when_at":"2026-05-02T15:00:00","pre_notify_minutes":5}}}}
    "내일 3시 회의 30분 전이랑 5분 전 둘 다 알림"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"회의","when_at":"2026-05-02T15:00:00","pre_notify_minutes":[30,5]}}}}
    "매일 오전 7시 운동 알람"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"운동","when_at":"2026-05-02T07:00:00","rrule_freq":"daily"}}}}
    "매주 월수금 오전 9시 주간 리뷰"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"주간 리뷰","when_at":"2026-05-04T09:00:00","rrule_freq":"weekly","rrule_byday":"MO,WE,FR"}}}}
    "매달 1일 9시 월간 리포트, 6개월간"
      → {{"tool":"schedule_bot","args":{{"action":"add","title":"월간 리포트","when_at":"2026-06-01T09:00:00","rrule_freq":"monthly","rrule_until":"2026-12-01T09:00:00"}}}}
    "다가오는 일정 보여줘" / "내 일정 뭐 있어?"
      → {{"tool":"schedule_bot","args":{{"action":"upcoming"}}}}
    "3번 일정 삭제" / "#3 지워줘"
      → {{"tool":"schedule_bot","args":{{"action":"delete","event_id":3}}}}

### 3. finance_bot(action, indicator?)
- 목적: 외부 경제 지표 조회 + 사용자의 wiki 투자 원칙과 대조 평가
- action ∈ {{"latest", "dashboard", "compare_with_principles"}}
- 인자:
    latest    → indicator 필수. 허용값: 기준금리, CPI, USD/KRW, FED, DGS10, VIX
    dashboard → 인자 없음 (위 6개 지표 한 번에)
    compare_with_principles → 인자 없음 (지표 + wiki 원칙 → 31B 통합 평가)
- 예시:
    "지금 환율 얼마야?" / "원달러 환율 알려줘"
      → {{"tool":"finance_bot","args":{{"action":"latest","indicator":"USD/KRW"}}}}
    "VIX 지금 몇이야"
      → {{"tool":"finance_bot","args":{{"action":"latest","indicator":"VIX"}}}}
    "경제 지표 한번 보여줘" / "대시보드"
      → {{"tool":"finance_bot","args":{{"action":"dashboard"}}}}
    "지금 시장이 내 매매 원칙이랑 어떻게 맞아?" / "포지션 점검 필요해?"
      → {{"tool":"finance_bot","args":{{"action":"compare_with_principles"}}}}

### 4. invest_bot(action, ticker_or_name?)
- 목적: 한국 주식 단일 종목 차트·기술적 지표 분석. (실주문 절대 안 함, 신호만 산출)
- action ∈ {{"analyze", "compare_with_rules"}}
- 인자:
    analyze            → ticker_or_name 필수. 지표·신호만 (LLM 무호출, 빠름)
    compare_with_rules → ticker_or_name 필수. 지표 + Wiki 매매 원칙 → 31B 평가
- 예시:
    "삼성전자 차트 봐줘" / "005930 분석"
      → {{"tool":"invest_bot","args":{{"action":"analyze","ticker_or_name":"삼성전자"}}}}
    "삼성전자 지금 매수 조건 충족해?" / "내 원칙 기준 평가해"
      → {{"tool":"invest_bot","args":{{"action":"compare_with_rules","ticker_or_name":"삼성전자"}}}}

### 5. kium_bot(action, top_n?, market?, with_crash_signals?)
- 목적: 시장 universe(KOSPI200 등) 12-1 모멘텀 Top N 스캔 + (옵션) 3중 크래시 감지·VKOSPI 비중 권고
- action: "scan" 만
- 인자:
    top_n               (선택, 정수, 기본 10) — 상위 N개
    market              (선택, 기본 "KOSPI200") — "KOSPI200" / "KOSDAQ150" / "KOSPI200+KOSDAQ150"
    with_crash_signals  (선택, bool, 기본 false) — true면 변동성·시장패닉·모멘텀역전 감지 + VKOSPI 비중
- mode: 항상 fast (LLM 무호출, 순수 계산)
- 예시:
    "키움봇 모멘텀 스캔" / "오늘 모멘텀 좋은 종목" / "Top 10 모멘텀"
      → {{"tool":"kium_bot","args":{{"action":"scan"}},"mode":"fast"}}
    "코스닥150 Top 20"
      → {{"tool":"kium_bot","args":{{"action":"scan","top_n":20,"market":"KOSDAQ150"}},"mode":"fast"}}
    "지금 시장 위험해? 크래시 감지 같이" / "모멘텀 스캔 + 시장 점검"
      → {{"tool":"kium_bot","args":{{"action":"scan","with_crash_signals":true}},"mode":"fast"}}

### 6. quant_bot(action, months?, top_n?, market?, phase_override?)
- 목적: 거시 국면(MSCI 4분면) + 국면별 종목 팩터 스코어링 Top N 추천.
- action ∈ {{"phase", "recommend"}}
    "phase"     — 거시 국면만 (v3.22)
    "recommend" — 국면 + Top N 종목 추천 (v3.23). 5팩터 z-score 가중합 결정론.
- 인자:
    months          (선택, 정수, 기본 24, 18~60 클램프) — 시계열 길이
    top_n           (선택, recommend 시. 1~30. 미지정 시 phase별 디폴트 Recovery/Expansion 8 / Slowdown 6 / Contraction 4)
    market          (선택, recommend 시. "KOSPI200"/"KOSDAQ150"/"KOSPI200+KOSDAQ150", 기본 KOSPI200)
    phase_override  (선택, recommend 시. 거시 국면을 무시하고 강제 phase로 추천)
- mode: 항상 fast (LLM 무호출, 결정론 z-score)
- 예시:
    "지금 경기 국면 어때?" / "콴텍봇 국면 보여줘"
      → {{"tool":"quant_bot","args":{{"action":"phase"}},"mode":"fast"}}
    "콴텍봇 종목 추천" / "지금 국면에 맞는 Top N" / "월간 리밸런싱"
      → {{"tool":"quant_bot","args":{{"action":"recommend"}},"mode":"fast"}}
    "콴텍봇 Top 5" / "5종목만 추천"
      → {{"tool":"quant_bot","args":{{"action":"recommend","top_n":5}},"mode":"fast"}}
    "Recovery 가정해서 코스닥 추천" / "강제로 Expansion 추천"
      → {{"tool":"quant_bot","args":{{"action":"recommend","phase_override":"Recovery","market":"KOSDAQ150"}},"mode":"fast"}}

### 7. inbox_bot(content, hint?)
- 목적: 사용자가 "메모로 저장하고 싶다"는 명백한 의도를 드러낸 경우 raw/inbox/에 저장
- 인자: content (저장할 본문, 메타 동사 제거), hint (파일명 prefix, 예: "투자_관찰")
- ⚠️ 보수적 분기: 사용자가 정보를 요청하는지(질문) vs 저장을 요청하는지 헷갈리면 knowledge_bot. 명백히 "기록해/메모해/일지에 추가/저장" 동사가 있을 때만.
- 예시:
    "삼성전자 26.4Q 영업이익 컨센 상회. 다음 분기 가이던스 주목하라고 메모해줘"
      → {{"tool":"inbox_bot","args":{{"content":"삼성전자 26.4Q 영업이익 컨센 상회. 다음 분기 가이던스 주목","hint":"투자_관찰"}}, "mode":"fast"}}
    "오늘 본 책 핵심: ... — 이거 기록"
      → {{"tool":"inbox_bot","args":{{"content":"...","hint":"독서"}}, "mode":"fast"}}
- 잘못된 예 (분기 금지):
    "삼성전자 영업이익 어때?" → knowledge_bot or invest_bot (질문이지 메모 아님)
    "내 매매 청산 규칙 뭐야?" → knowledge_bot (조회 의도)

### 8. coding_bot(action, content, language?, files?)
- 목적: 코드 설계·작성·디버깅·리뷰. fast=Qwen2.5-Coder 32B 로컬, accurate=Claude API.
- action ∈ {{"design", "code", "debug", "review"}}
- 인자:
    content   필수. 요구사항·명세·에러 메시지·검토 대상 코드 등.
    language  선택. "python", "javascript" 등.
    files     선택. [{{"name":"foo.py","text":"..."}}, ...] (debug/review 시 첨부)
- mode 가이드:
    debug, 프로덕션 코드, 보안 관련 → accurate (Claude)
    간단한 스니펫·아이디어 스케치 → fast (Qwen 로컬)
- 예시:
    "파이썬으로 장바구니 클래스 설계해줘"
      → {{"tool":"coding_bot","args":{{"action":"design","content":"파이썬 장바구니 클래스","language":"python"}},"mode":"accurate"}}
    "이 함수 구현해 — input N → 피보나치 N번째"
      → {{"tool":"coding_bot","args":{{"action":"code","content":"피보나치 N번째 (input N)"}},"mode":"fast"}}
    "피보나치 N번째 빠르게 짜줘" / "정렬 함수 만들어줘" / "Hello World 파이썬으로"
      → {{"tool":"coding_bot","args":{{"action":"code","content":"피보나치 N번째"}},"mode":"fast"}}
    "이진 탐색 구현해" / "URL 파싱하는 함수 짜줘" / "스택 클래스 만들어"
      → {{"tool":"coding_bot","args":{{"action":"code","content":"이진 탐색 구현"}},"mode":"fast"}}
    "이 에러 왜 나? TypeError: NoneType has no len()"
      → {{"tool":"coding_bot","args":{{"action":"debug","content":"TypeError: NoneType has no len()"}},"mode":"accurate"}}
    "이 코드 리뷰해줘" + 파일 첨부
      → {{"tool":"coding_bot","args":{{"action":"review","content":"코드 리뷰"}},"mode":"accurate"}}
- 잘못된 예 (분기 금지):
    "피보나치 짜줘" → knowledge_bot ❌  wiki/에 알고리즘·언어 일반 지식 노트 없음. 반드시 coding_bot.
    "정렬 함수 만들어줘" → knowledge_bot ❌  코드 생성 요청. coding_bot으로.
    ※ 코드 생성·구현·디버깅 의도가 보이면 wiki 검색이 무의미함. 항상 coding_bot.

### 9. ipo_bot(action, corp_name?, days_ahead?, top_n?, ...)
- 목적: 공모주 청약 일정 수집 + 매력지수 5요소 산출 (KIND/38/DART 연동)
- 매력지수 5요소: 수요예측 경쟁률 / 공모가 밴드 위치 / 유통 비율 / 주관사 티어 / 공모 규모(시총)
- 사용 케이스: "다음달 공모주 뭐 있어?", "XX 공모주 매력지수 계산해줘", "IPO 스캔해줘"
- action="scan": 향후 days_ahead일 공모주 자동 스캔 + 매력지수 Top N
- action="analyze": 단일 종목 수동 입력 분석 (corp_name 필수; 확약%→lockup_ratio)
- 예시:
  "다음달 공모주 일정" → {{"tool":"ipo_bot","args":{{"action":"scan","days_ahead":30}},"mode":"fast"}}
  "OO 공모주 경쟁률 1200 밴드 13000~15000 확정 16000 시총 800억 확약 78% 미래에셋" →
    {{"tool":"ipo_bot","args":{{"action":"analyze","corp_name":"OO","competition_rate":1200,
     "band_low":13000,"band_high":15000,"final_price":16000,"market_cap":800,"lockup_ratio":78,"underwriter":"미래에셋증권"}},"mode":"fast"}}

### 10. news_bot()
- 목적: IT/AI 뉴스(Anthropic·DeepMind·한경IT·NAVER·삼성) 최신 기사 헤드라인+3줄 요약
- 사용 케이스: "IT 뉴스 요약", "오늘 뉴스 브리핑", "AI 뉴스 정리해줘", "뉴스 보여줘"
- 예시: "IT 뉴스 요약해줘" → {{"tool":"news_bot","args":{{}},"mode":"fast"}}

### 10b. action_schedule(op, action?, freq?, time?, weekday?, until?, id?)
- 목적: 봇이 정해진 시각에 자동 실행할 반복작업 등록/조회/삭제(뉴스·신호·공모주·국면)
- 사용 케이스: "매일 8시 뉴스 보내", "매주 월 9시 공모주 스캔", "자동작업 목록", "자동작업 2 삭제"
- 예시: "매일 8시 IT뉴스 보내 6/11까지" → {{"tool":"action_schedule","args":{{"op":"add","action":"news","freq":"daily","time":"08:00","until":"2026-06-11"}},"mode":"fast"}}

### 10c. system_info(topic)
- 목적: 봇 자기/시스템 상태 질문에 실제 상태로 답변(접속주소·서비스·봇목록·자동작업·신호/리밸런싱/스캔 실행여부)
- topic ∈ {{"access","service","bots","automation","signal","rebalance","scan","performance","feedback","analysis","status"}}
- 사용 케이스: "paper 주소 뭐야", "무슨 봇 돌고 있어", "마지막 신호 언제", "리밸런싱 됐어?"
- 예시: "paper 주소 뭐야" → {{"tool":"system_info","args":{{"topic":"access"}},"mode":"fast"}}

### 11. respond_directly(answer: string)
- 목적: 도구 호출 없이 즉시 답변
- 사용 케이스: 인사, 잡담, 봇 자체에 대한 메타 질문, 단순 시간/날짜/일반 상식
- 예시: "안녕", "너 누구야?", "고마워"


## mode 필드 (속도/정확도 자동 판단)

모든 응답에 mode 필드 필수. 라우터가 입력의 정확도 요구를 판단해 결정한다.

- "fast": LLM 호출 최소화/생략 또는 짧은 컨텍스트. 즉답·지표 조회·일정 등록·계산·시각 조회에 사용.
- "accurate": 31B 풀 추론. 매매 판단·원칙 대조·복잡한 분석·돈 직결 결정에 사용.

### 판단 알고리즘 (우선순위 순서대로 적용)

**[Level 1] 사용자 명시 요청 — 무조건 따른다**
- "정확하게/자세하게/꼼꼼하게/제대로/원칙대로/정밀하게/풀로/심층/근거 들어서" → accurate
- "빠르게/간단히/짧게/대충/요약만/그냥/한 줄/간략히" → fast

**[Level 2] 도구 기반 휴리스틱**
- respond_directly (인사·잡담·시각/날짜 조회) → fast
- schedule_bot 모든 action → fast (DB 작업, LLM 무관)
- finance_bot.latest / finance_bot.dashboard → fast (단순 조회)
- finance_bot.compare_with_principles → accurate (포지션 점검)
- invest_bot.analyze → fast (지표만, LLM 무호출)
- invest_bot.compare_with_rules → accurate (매매 평가)
- inbox_bot → fast (저장 작업)
- kium_bot.scan → fast (지표 계산만, LLM 무호출)
- quant_bot.phase → fast (거시지표 → 국면 분류, LLM 무호출)
- quant_bot.recommend → fast (z-score 가중합 종목 추천, LLM 무호출)

**[Level 3] knowledge_bot 세부 분기 (도구 분류 후 필요할 때만 적용)**
- accurate 트리거 키워드: "매매·매수·매도·진입·청산·손절·익절·포지션·리스크·평가·판단·결정·분석·검토·돈·자산·전략·근거·원칙대로·정확히·자세히·왜"
- fast 트리거 키워드: "뭐야·뭐임·몇이야·어디·어떻게(개념)·간단히·요약·예시"
- 둘 다 없거나 충돌 시: knowledge_bot은 기본 accurate (지식 조회는 정확성 중시)

**[Level 4] 한 줄 룰 (위에서 결정 안 났을 때)**
"틀렸을 때 사용자가 손해를 보는가?" Yes면 accurate, No면 fast.

### 예시

| 입력 | mode | 적용된 레벨 |
|---|---|---|
| "내 매매 청산 규칙은?" | accurate | L3 (매매 키워드) |
| "RSI가 뭐야?" | fast | L3 (뭐야) |
| "RSI가 뭐야 자세히 설명해" | accurate | L1 (자세히) |
| "삼성전자 매수 조건 충족?" | accurate | L2 (compare_with_rules) |
| "삼성전자 차트 봐줘" | fast | L2 (analyze) |
| "VIX 지금 몇이야?" | fast | L2 (latest) |
| "지금 시장이 내 원칙에 맞아?" | accurate | L2 (compare_with_principles) |
| "내일 9시 회의 잡아줘" | fast | L2 (schedule_bot) |
| "안녕" | fast | L2 (respond_directly) |
| "포지션 점검 빠르게" | fast | L1 우선 (빠르게) |
| "이거 메모해줘 ..." | fast | L2 (inbox_bot) |

## 응답 형식

정확히 다음 JSON 한 줄만 출력. 다른 텍스트 절대 금지.

```json
{{"tool": "knowledge_bot", "args": {{"query": "검색용 명료한 한국어 질문"}}}}
```
또는
```json
{{"tool": "schedule_bot", "args": {{"action": "add", "title": "...", "when_at": "...", "rrule_freq": "weekly", "rrule_byday": "MO,WE,FR", "pre_notify_minutes": 5}}, "mode": "fast"}}
```
또는
```json
{{"tool": "finance_bot", "args": {{"action": "latest", "indicator": "USD/KRW"}}, "mode": "fast"}}
```
또는
```json
{{"tool": "invest_bot", "args": {{"action": "compare_with_rules", "ticker_or_name": "삼성전자"}}, "mode": "accurate"}}
```
또는
```json
{{"tool": "inbox_bot", "args": {{"content": "삼성전자 26.4Q 영업이익 ...", "hint": "투자_관찰"}}, "mode": "fast"}}
```
또는
```json
{{"tool": "coding_bot", "args": {{"action": "debug", "content": "TypeError: ...", "language": "python"}}, "mode": "accurate"}}
```
또는
```json
{{"tool": "respond_directly", "args": {{"answer": "직접 답변 텍스트"}}, "mode": "fast"}}
```

## 판단 규칙

1. 코드·디버깅·에러·구현·"이 함수"·"리팩토링" 관련이면 coding_bot.
   ▸ 코딩 동사: "짜줘"·"만들어줘"·"구현해"·"작성해"·"코드"·"함수로"·"스크립트로"·"프로그램"·"디버깅"·"리뷰"·"리팩토링"
   ▸ 알고리즘·자료구조·언어 일반 지식(피보나치/정렬/스택/큐/이진 탐색/HashMap/URL 파싱 등)에 코딩 동사가 붙으면 → 항상 coding_bot.
   ▸ wiki/에는 사용자 투자 원칙·메모만 있어 코드/알고리즘 노트가 없음. 코드 작성 의도면 knowledge_bot으로 절대 보내지 말 것.
2. "메모해/기록해/일지에 추가/저장해줘" 등 명백한 저장 의도 → inbox_bot. 헷갈리면 inbox_bot 안 씀.
3. 한국 주식 단일 종목 분석·매매 조건 평가 → invest_bot.
4. 환율·금리·CPI·VIX·"경제지표"·"포지션 점검" 같이 외부 시장 지표가 필요한 질문이면 finance_bot.
5. 일정·약속·미팅·알림 관련이면 schedule_bot. ("잡아줘", "등록", "내 일정", "#N 삭제" 등)
6. 그 외 사용자의 지식·원칙·메모 관련이면 knowledge_bot.
7. 멀티턴 컨텍스트(직전 대화)를 보고 모호한 표현("그거", "방금", "더") 해석.
8. respond_directly는 wiki·일정·지표 참조가 명백히 불필요한 경우만 (인사/잡담).
9. JSON 외 어떤 설명·코드블록도 추가하지 않음.
"""


_KOR_WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


def _build_system_prompt() -> str:
    now = datetime.now()
    return ROUTER_SYSTEM_PROMPT_TEMPLATE.format(
        now_kst=now.strftime("%Y-%m-%d %H:%M:%S"),
        weekday_kor=_KOR_WEEKDAYS[now.weekday()],
    )


# ─── 메인 함수 ───────────────────────────────────────


def route(query: str, history: list[dict] | None = None, model: str = MASTER_MODEL) -> dict:
    """사용자 입력 + 직전 대화 → {"tool": ..., "args": {...}}.

    파라미터:
      query:   사용자 새 메시지
      history: 직전 대화 [{role, content}, ...]. 없으면 None.
      model:   라우터 LLM (기본 gemma4:26b)

    반환:
      {"tool": "knowledge_bot", "args": {"query": "..."}}
      {"tool": "schedule_bot",  "args": {"action": "...", ...}}
      {"tool": "respond_directly", "args": {"answer": "..."}}
    """
    # v3.39: 구조화된 IPO analyze 질문은 LLM 없이 결정론적으로 단락 처리.
    # (26B 라우터가 잘못된 JSON을 뱉어 knowledge_bot으로 early-return 하는 실환경 문제 우회.
    #  LLM 호출/지연도 없어 더 빠르고 재현 가능.)
    _as = _detect_action_schedule(query)
    if _as:
        log.info(f"🎯 action_schedule short-circuit: {_as.get('op')} (정규식, LLM 미호출)")
        return {"tool": "action_schedule", "args": _as, "mode": "fast"}
    _si = _detect_system_info(query)
    if _si:
        log.info(f"🎯 system_info short-circuit: {_si.get('topic')} (정규식, LLM 미호출)")
        return {"tool": "system_info", "args": _si, "mode": "fast"}
    if _detect_news(query):
        log.info("🎯 news_bot short-circuit (정규식, LLM 미호출)")
        return {"tool": "news_bot", "args": {}, "mode": "fast"}
    ipo_forced = _detect_ipo_analyze(query)
    if ipo_forced:
        log.info(f"🎯 ipo_bot short-circuit: {ipo_forced.get('corp_name')} (정규식, LLM 미호출)")
        valid = _validate_ipo_args(ipo_forced)
        if valid:
            return {"tool": "ipo_bot", "args": valid, "mode": "fast"}

    messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": query})

    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "format": "json",  # Ollama strict JSON (Gemma 4 호환)
        "options": {
            "temperature": 0.0,  # 결정론적
            "num_ctx": 16384,
            "num_predict": 1024,  # JSON 출력 한도. format=json strict 모드가 한도 내 닫는 } 못 만들면 빈 응답으로 떨어짐 → 여유 있게.
        },
    }).encode("utf-8")

    req = Request(
        f"{OLLAMA_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.exception(f"라우터 호출 실패: {e}")
        return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}

    text = resp.get("message", {}).get("content", "").strip()
    if not text:
        log.warning(
            f"라우터 빈 응답 — eval_count={resp.get('eval_count')} "
            f"prompt_eval={resp.get('prompt_eval_count')} "
            f"done_reason={resp.get('done_reason')!r} "
            f"(num_predict 한도 부족 또는 strict JSON 파서 충돌 가능)"
        )
    log.debug(f"라우터 raw 출력: {text[:300]}")

    action = _parse_json(text)
    if action is None:
        log.warning(f"라우터 JSON 파싱 실패 → knowledge_bot fallback. raw: {text[:200]!r}")
        return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}

    tool = action.get("tool")
    args = action.get("args") or {}
    mode = (action.get("mode") or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        log.debug(f"알 수 없는 mode '{mode}' → {DEFAULT_MODE} fallback")
        mode = DEFAULT_MODE
    # B-2: 사용자 명시 키워드로 mode 결정론 override
    mode = _override_mode_by_keywords(query, mode)

    # v3.38: IPO analyze 결정론 override — LLM이 tool/corp_name을 놓쳐도 강제 라우팅
    ipo_forced = _detect_ipo_analyze(query)
    if ipo_forced and tool != "ipo_bot":
        log.info(f"🎯 ipo_bot override: {tool} → ipo_bot/analyze (정규식 단서)")
        tool = "ipo_bot"
        args = ipo_forced
    elif ipo_forced and tool == "ipo_bot":
        # LLM이 ipo_bot은 맞췄으나 일부 필드/ corp_name 누락 → 정규식으로 보강
        args = {**args, **ipo_forced}

    if tool not in KNOWN_TOOLS:
        log.warning(f"알 수 없는 tool '{tool}' → knowledge_bot fallback")
        return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}

    # 도구별 args 검증·보정
    if tool == "knowledge_bot":
        if not args.get("query"):
            args = {"query": query}

    elif tool == "schedule_bot":
        valid = _validate_schedule_args(args)
        if not valid:
            log.warning(f"schedule_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "finance_bot":
        valid = _validate_finance_args(args)
        if not valid:
            log.warning(f"finance_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "invest_bot":
        valid = _validate_invest_args(args)
        if not valid:
            log.warning(f"invest_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "kium_bot":
        valid = _validate_kium_args(args)
        if not valid:
            log.warning(f"kium_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "quant_bot":
        valid = _validate_quant_args(args)
        if not valid:
            log.warning(f"quant_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "ipo_bot":
        # v3.37: analyze는 결정론 정규식으로 숫자/주관사 필드 보강(정규식 우선)
        if (args.get("action") or "scan").strip().lower() == "analyze":
            rx = _extract_ipo_fields_from_query(query)
            if rx:
                args = {**args, **rx}
        valid = _validate_ipo_args(args)
        if not valid:
            log.warning(f"ipo_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "news_bot":
        args = _validate_news_args(args)

    elif tool == "system_info":
        args = _validate_system_info(args)

    elif tool == "action_schedule":
        valid = _validate_action_schedule(args)
        if not valid:
            log.warning(f"action_schedule args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "inbox_bot":
        valid = _validate_inbox_args(args)
        if not valid:
            log.warning(f"inbox_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "coding_bot":
        valid = _validate_coding_args(args)
        if not valid:
            log.warning(f"coding_bot args 유효성 실패 → knowledge_bot fallback. args: {args}")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}
        args = valid

    elif tool == "respond_directly":
        if not args.get("answer"):
            log.warning("respond_directly인데 answer 없음 → knowledge_bot fallback")
            return {"tool": "knowledge_bot", "args": {"query": query}, "mode": DEFAULT_MODE}

    log.info(f"🧭 라우팅: {tool} mode={mode} args={str(args)[:140]}")
    return {"tool": tool, "args": args, "mode": mode}


# ─── schedule_bot args 검증 ──────────────────────────


def _validate_schedule_args(args: dict) -> dict | None:
    """라우터가 채운 schedule_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "").strip().lower()
    if action not in SCHEDULE_ACTIONS:
        return None

    out: dict = {"action": action}

    if action == "add":
        title = (args.get("title") or "").strip()
        when_at = (args.get("when_at") or "").strip()
        if not title or not when_at:
            return None
        out["title"] = title
        out["when_at"] = when_at
        if args.get("notes"):
            out["notes"] = str(args["notes"]).strip()

        # RRULE 옵션 (라우터가 자연어 → enum 변환 책임)
        freq = (args.get("rrule_freq") or "").strip().lower()
        if freq in ("daily", "weekly", "monthly"):
            out["rrule_freq"] = freq
            byday = (args.get("rrule_byday") or "").strip().upper()
            if byday and freq == "weekly":
                # 'MO,WE,FR' 같은 형식 검증 — 알 수 없는 토큰 제거
                tokens = [t.strip() for t in byday.split(",")]
                valid = [t for t in tokens if t in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}]
                if valid:
                    out["rrule_byday"] = ",".join(valid)
            until = (args.get("rrule_until") or "").strip()
            if until:
                out["rrule_until"] = until

        # 사전 알림 — int (단일) 또는 list[int] (멀티, v3.15)
        pn = args.get("pre_notify_minutes")
        if pn is not None:
            if isinstance(pn, (list, tuple, set)):
                # list — 양수 int만 추리고 unique 정렬 (큰 값 먼저)
                cleaned = set()
                for x in pn:
                    try:
                        v = int(x)
                        if v >= 1:
                            cleaned.add(v)
                    except (TypeError, ValueError):
                        continue
                if cleaned:
                    out["pre_notify_minutes"] = sorted(cleaned, reverse=True)
            else:
                try:
                    pn_val = int(pn)
                    if pn_val >= 1:
                        out["pre_notify_minutes"] = pn_val
                except (TypeError, ValueError):
                    pass

        # 충돌 감지 윈도우 (v3.14) — 라우터가 명시 안 하면 schedule_bot 디폴트(15분)
        # 사용자가 "충돌 검사 끄고/같은 시간 다른 일정도 OK" 의도면 0 허용.
        cwm = args.get("conflict_window_minutes")
        if cwm is not None:
            try:
                cwm_val = int(cwm)
                if cwm_val >= 0:
                    out["conflict_window_minutes"] = cwm_val
            except (TypeError, ValueError):
                pass
        return out

    if action in ("delete", "complete"):
        eid = args.get("event_id")
        try:
            out["event_id"] = int(eid)
        except (TypeError, ValueError):
            return None
        return out

    # list / upcoming — 추가 인자 불필요
    if "limit" in args:
        try:
            out["limit"] = max(1, min(100, int(args["limit"])))
        except (TypeError, ValueError):
            pass
    return out


# ─── finance_bot args 검증 ───────────────────────────


def _validate_finance_args(args: dict) -> dict | None:
    """라우터가 채운 finance_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "").strip().lower()
    if action not in FINANCE_ACTIONS:
        return None

    out: dict = {"action": action}

    if action == "latest":
        ind = (args.get("indicator") or "").strip()
        if not ind:
            return None
        canonical = None
        for known in FINANCE_INDICATORS:
            if known.lower() == ind.lower():
                canonical = known
                break
        if canonical is None:
            return None
        out["indicator"] = canonical
        return out

    return out


# ─── invest_bot args 검증 ────────────────────────────


def _validate_invest_args(args: dict) -> dict | None:
    """라우터가 채운 invest_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "").strip().lower()
    if action not in INVEST_ACTIONS:
        return None
    out: dict = {"action": action}
    ton = (args.get("ticker_or_name") or "").strip()
    if not ton:
        return None
    out["ticker_or_name"] = ton
    return out


# ─── ipo_bot analyze 인자 결정론 정규식 추출 (v3.37) ──────
# 26B 라우터 LLM이 한국어 자연어에서 IPO 숫자 필드를 안정적으로 못 뽑는 문제
# (실환경 실측: 필드 여러 개 줘도 1~3개만 추출) → v3.13 mode override 패턴처럼
# 정규식으로 직접 추출해 LLM args를 보강한다(정규식이 찾은 필드는 정규식 값 우선).

_IPO_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"


def _ipo_f(s: str) -> float:
    return float(s.replace(",", ""))


_IPO_BAND_RE  = re.compile(r"\ubc34\ub4dc\s*" + _IPO_NUM + r"\s*[~\u223c\-\u2013\u2014]\s*" + _IPO_NUM)
_IPO_COMP_RE  = re.compile(r"\uacbd\uc7c1\ub960\s*" + _IPO_NUM)
_IPO_FINAL_RE = re.compile(r"(?:\ud655\uc815(?:\uacf5\ubaa8\uac00|\uac00)?|\uacf5\ubaa8\uac00)\s*" + _IPO_NUM)
_IPO_FLOAT_RE = re.compile(r"\uc720\ud1b5\s*\ube44\uc728\s*" + _IPO_NUM)
_IPO_LOCK_RE  = re.compile(r"(?:\uc758\ubb34\ubcf4\uc720\s*)?\ud655\uc57d\s*" + _IPO_NUM)
_IPO_CAP_RE   = re.compile(r"(?:\uc2dc\uac00\ucd1d\uc561|\uc2dc\ucd1d)\s*" + _IPO_NUM + r"\s*\uc5b5")

# \uc8fc\uad00\uc0ac: ipo_bot \ud2f0\uc5b4 \uc0ac\uc804 \ud0a4 + 'OO\uc99d\uad8c' \uc77c\ubc18\ud615. \uae34 \uc774\ub984 \uba3c\uc800 \ub9e4\uce6d.
try:
    from ipo_bot import _UNDERWRITER_TIER as _IPO_UW_TIER  # type: ignore
    _IPO_UW_NAMES = sorted(_IPO_UW_TIER.keys(), key=len, reverse=True)
except Exception:  # pragma: no cover - ipo_bot \ubbf8\ub85c\ub529 \ud658\uacbd \uc548\uc804\ub9dd
    _IPO_UW_NAMES = []
_IPO_UW_RE = re.compile(
    "(" + "|".join(re.escape(n) for n in _IPO_UW_NAMES) + r"|[\uac00-\ud7a3A-Za-z]+\uc99d\uad8c)"
) if _IPO_UW_NAMES else re.compile(r"([\uac00-\ud7a3A-Za-z]+\uc99d\uad8c)")


def _extract_ipo_fields_from_query(query: str) -> dict:
    """\uc790\uc5f0\uc5b4\uc5d0\uc11c IPO analyze \uc22b\uc790/\uc8fc\uad00\uc0ac \ud544\ub4dc\ub97c \uacb0\uc815\ub860\uc801\uc73c\ub85c \ucd94\ucd9c."""
    out: dict = {}
    if not query:
        return out
    m = _IPO_COMP_RE.search(query)
    if m:
        out["competition_rate"] = _ipo_f(m.group(1))
    m = _IPO_BAND_RE.search(query)
    if m:
        out["band_low"] = _ipo_f(m.group(1))
        out["band_high"] = _ipo_f(m.group(2))
    m = _IPO_FLOAT_RE.search(query)
    if m:
        out["float_ratio"] = _ipo_f(m.group(1))
    m = _IPO_LOCK_RE.search(query)
    if m:
        out["lockup_ratio"] = _ipo_f(m.group(1))
    m = _IPO_CAP_RE.search(query)
    if m:
        out["market_cap"] = _ipo_f(m.group(1))
    m = _IPO_UW_RE.search(query)
    if m:
        out["underwriter"] = m.group(1)
    # \ud655\uc815\uacf5\ubaa8\uac00: \ubc34\ub4dc \ub9e4\uce6d \uad6c\uac04\uc744 \uc9c0\uc6b4 \ub4a4 \uac80\uc0c9(\ubc34\ub4dc \uc22b\uc790 \uc624\uc778\uc2dd \ubc29\uc9c0)
    m = _IPO_FINAL_RE.search(_IPO_BAND_RE.sub(" ", query))
    if m:
        out["final_price"] = _ipo_f(m.group(1))
    return out


# v3.38: IPO analyze 결정론 라우팅 — LLM이 tool/corp_name을 놓쳐 knowledge_bot으로
# 빠지는 실환경 문제 보완. "<종목> 공모주 ... 매력지수" 형태면 LLM 판단과 무관하게
# ipo_bot/analyze로 강제하고 corp_name도 정규식으로 추출한다.
_IPO_CORP_RE = re.compile(r"([\uac00-\ud7a3A-Za-z0-9]+)\s*(?:\uacf5\ubaa8\uc8fc|\ub9e4\ub825\uc9c0\uc218|\ub9e4\ub825\ub3c4)")
# IPO 분석 의도 단서 (이 중 하나 이상 있어야 강제 발동)
_IPO_CUE_RE = re.compile(r"\uacf5\ubaa8\uc8fc|\ub9e4\ub825\uc9c0\uc218|\ub9e4\ub825\ub3c4|IPO|\uccad\uc57d")


def _extract_ipo_corp_name(query: str) -> str | None:
    if not query:
        return None
    m = _IPO_CORP_RE.search(query)
    return m.group(1) if m else None


def _detect_ipo_analyze(query: str) -> dict | None:
    """IPO analyze로 강제할지 결정. 확신 시 analyze args dict, 아니면 None.

    조건: (1) IPO 분석 단서 존재 + (2) corp_name 추출 가능 + (3) 숫자 필드 2개 이상.
    숫자 2개 이상 = scan/잡담과의 오발동 방지 임계값.
    """
    if not query or not _IPO_CUE_RE.search(query):
        return None
    fields = _extract_ipo_fields_from_query(query)
    numeric = [k for k in fields if k != "underwriter"]
    corp = _extract_ipo_corp_name(query)
    if corp and len(numeric) >= 2:
        return {"action": "analyze", "corp_name": corp, **fields}
    return None


# \u2500\u2500\u2500 ipo_bot args \uac80\uc99d (v3.26) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


# ─── action_schedule (자연어 봇작업 예약, v3.43) ─────
from datetime import datetime as _dt_now  # noqa: E402
_AS_WD = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_AS_RECUR_RE = re.compile(r"매일|매주|평일|날마다|아침마다|저녁마다|시마다")
_AS_ACTIONS = {"news", "signal", "ipo", "quant", "performance"}


def _as_parse_time(t: str):
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"(오전|오후|아침|저녁|밤|낮)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", t)
    if m:
        ap, h = m.group(1), int(m.group(2))
        mi = int(m.group(3)) if m.group(3) else 0
        if ap in ("오후", "저녁", "밤") and h < 12:
            h += 12
        if ap in ("오전", "아침") and h == 12:
            h = 0
        return f"{h:02d}:{mi:02d}"
    return None


def _as_parse_until(t: str):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s*까지", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})\s*[/월]\s*(\d{1,2})\s*일?\s*까지", t)
    if m:
        return f"{_dt_now.now().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _as_weekday(t: str):
    m = re.search(r"매주\s*([월화수목금토일])", t) or re.search(r"([월화수목금토일])요일", t)
    return _AS_WD[m.group(1)] if m else None


def _as_action_key(t: str):
    tl = t.lower()
    if "뉴스" in t or "news" in tl:
        return "news"
    if "신호" in t:
        return "signal"
    if "공모주" in t or "ipo" in tl or "아이피오" in t:
        return "ipo"
    if "국면" in t or "거시" in t or "콴텍" in t:
        return "quant"
    if "성과" in t or "수익률" in t or "승률" in t:
        return "performance"
    return None


def _detect_action_schedule(query: str):
    """자연어 → 봇작업 예약 op dict. 아니면 None."""
    if not query:
        return None
    if re.search(r"자동\s*작업|자동화|예약\s*작업|스케줄", query) and re.search(r"목록|뭐 ?있|보여|리스트|알려", query):
        return {"op": "list"}
    m = re.search(r"(?:자동\s*작업|예약|스케줄|작업)\s*#?\s*(\d+)\s*(?:번)?\s*(삭제|취소|지워|중지|꺼)", query)
    if m:
        op = "disable" if ("중지" in m.group(2) or "꺼" in m.group(2)) else "delete"
        return {"op": op, "id": int(m.group(1))}
    action = _as_action_key(query)
    if not action or not _AS_RECUR_RE.search(query):
        return None
    t = _as_parse_time(query)
    if not t:
        return None
    wd = _as_weekday(query)
    freq = "weekly" if ("매주" in query and wd is not None) else "daily"
    out = {"op": "add", "action": action, "freq": freq, "time": t}
    if freq == "weekly" and wd is not None:
        out["weekday"] = wd
    u = _as_parse_until(query)
    if u:
        out["until"] = u
    return out


def _validate_action_schedule(args: dict):
    op = (args.get("op") or "").strip().lower()
    if op in ("list",):
        return {"op": "list"}
    if op in ("delete", "disable", "enable"):
        try:
            return {"op": op, "id": int(args.get("id"))}
        except (TypeError, ValueError):
            return None
    if op == "add":
        action = (args.get("action") or "").strip().lower()
        if action not in _AS_ACTIONS:
            return None
        freq = (args.get("freq") or "daily").strip().lower()
        if freq not in ("daily", "weekly"):
            freq = "daily"
        t = args.get("time")
        if not (isinstance(t, str) and re.match(r"^\d{2}:\d{2}$", t)):
            return None
        out = {"op": "add", "action": action, "freq": freq, "time": t}
        if freq == "weekly" and args.get("weekday") is not None:
            try:
                out["weekday"] = max(0, min(6, int(args["weekday"])))
            except (TypeError, ValueError):
                pass
        if args.get("until"):
            out["until"] = str(args["until"])
        return out
    return None


_NEWS_CUE_RE = re.compile(r"뉴스|news|브리핑|다이제스트|헤드라인")
_NEWS_ACT_RE = re.compile(r"요약|브리핑|다이제스트|정리|보여|알려|줘|업데이트|최신|헤드라인")


def _detect_news(query: str) -> bool:
    """IT/AI 뉴스 다이제스트 의도 결정론 감지."""
    if not query or not _NEWS_CUE_RE.search(query):
        return False
    return bool(_NEWS_ACT_RE.search(query))


def _validate_news_args(args: dict) -> dict:
    """news_bot은 필수 인자 없음. per_source만 선택 클램프."""
    out: dict = {}
    ps = args.get("per_source")
    if ps is not None:
        try:
            out["per_source"] = max(1, min(10, int(ps)))
        except (TypeError, ValueError):
            pass
    return out


# ─── system_info (봇 자기/시스템 인식, v3.45) ─────
_SI_TOPICS = {"access", "service", "bots", "automation",
              "signal", "rebalance", "scan", "performance", "feedback",
              "analysis", "status"}


def _detect_system_info(query: str):
    """봇 자기/시스템 상태 질문을 결정론적으로 감지. topic dict 또는 None.

    knowledge/news/research로 오발동하던 메타 질문을 실제 상태 응답기로 보낸다.
    action_schedule('자동작업 목록')보다 뒤에 호출되어 그쪽을 침범하지 않음.
    """
    if not query:
        return None
    q = query
    if re.search(r"매매\s*습관|손익비|매매\s*패턴|보유\s*기간|심화\s*분석", q) or \
            (re.search(r"분석", q) and
             re.search(r"매매|거래|트레이딩|paper|페이퍼|포트폴리오|성과|슬롯", q, re.I)):
        return {"topic": "analysis"}
    if re.search(r"(비중|배분|슬롯\s*비중).*(제안|조정|추천|바꿔|조절|튜닝)", q) or \
            re.search(r"리밸런싱\s*제안|성과.*비중\s*(제안|조정|추천)", q):
        return {"topic": "feedback"}
    if re.search(r"성과|수익률|승률|손익|벌었|잃었|얼마.*(벌|잃)", q) and \
            re.search(r"paper|페이퍼|슬롯|봇|콴텍|키움|ipo|전체|포트폴리오|내|우리|트레이딩", q, re.I):
        return {"topic": "performance"}
    if re.search(r"리밸런싱", q) and re.search(r"됐|완료|했|언제|반영|돌았|끝났", q):
        return {"topic": "rebalance"}
    if re.search(r"마지막.*신호|신호.*(언제|마지막|보냈|쐈|줬)", q):
        return {"topic": "signal"}
    if re.search(r"(마지막|최근|언제).*스캔|스캔.*(언제|마지막|했)|주간\s*스캔", q):
        return {"topic": "scan"}
    if re.search(r"서비스\s*상태|launchd|서비스.*(떠|실행|상태|돌)", q):
        return {"topic": "service"}
    if re.search(r"무슨\s*봇|어떤\s*봇|봇\s*목록|봇.*(돌고\s*있|실행\s*중)|"
                 r"돌고\s*있는\s*봇|실행\s*중인\s*봇|어떤\s*도구", q):
        return {"topic": "bots"}
    if re.search(r"주소|포트|로컬\s*사이트|사이트\s*주소|접속\s*정보", q):
        return {"topic": "access"}
    if re.search(r"paper", q, re.I) and re.search(
            r"주소|사이트|어디|접속|열|url|링크", q, re.I):
        return {"topic": "access"}
    return None


def _validate_system_info(args: dict) -> dict:
    """system_info args 정규화. 항상 유효한 dict 반환(실패 없음)."""
    topic = (args.get("topic") or "status").strip().lower()
    if topic not in _SI_TOPICS:
        topic = "status"
    return {"topic": topic}


def _validate_ipo_args(args: dict) -> dict | None:
    """라우터가 채운 ipo_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "scan").strip().lower()
    if action not in IPO_ACTIONS:
        return None
    out: dict = {"action": action}

    if action == "analyze":
        corp_name = (args.get("corp_name") or "").strip()
        if not corp_name:
            return None  # corp_name 필수
        out["corp_name"] = corp_name
        # 수치 파라미터 선택적
        for key in ("competition_rate", "band_low", "band_high", "final_price",
                    "float_ratio", "lockup_ratio", "market_cap"):
            v = args.get(key)
            if v is not None:
                try:
                    out[key] = float(v)
                except (TypeError, ValueError):
                    pass
        uw = (args.get("underwriter") or "").strip()
        if uw:
            out["underwriter"] = uw
        return out

    # action == "scan"
    days = args.get("days_ahead")
    if days is not None:
        try:
            d = int(days)
            if 1 <= d <= 90:
                out["days_ahead"] = d
        except (TypeError, ValueError):
            pass

    top_n = args.get("top_n")
    if top_n is not None:
        try:
            n = int(top_n)
            if 1 <= n <= 20:
                out["top_n"] = n
        except (TypeError, ValueError):
            pass

    return out


# ─── kium_bot args 검증 (v3.16) ──────────────────────


def _validate_kium_args(args: dict) -> dict | None:
    """라우터가 채운 kium_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "").strip().lower()
    if action not in KIUM_ACTIONS:
        return None
    out: dict = {"action": action}

    top_n = args.get("top_n")
    if top_n is not None:
        try:
            tn = int(top_n)
            if 1 <= tn <= 50:
                out["top_n"] = tn
        except (TypeError, ValueError):
            pass

    market = (args.get("market") or "").strip().upper()
    if market and market in KIUM_MARKETS:
        out["market"] = market

    # v3.17: 3중 크래시 감지 + VKOSPI 비중 권고 옵션
    wcs = args.get("with_crash_signals")
    if wcs is not None:
        if isinstance(wcs, bool):
            out["with_crash_signals"] = wcs
        elif isinstance(wcs, (int, float)):
            out["with_crash_signals"] = bool(wcs)
        elif isinstance(wcs, str):
            out["with_crash_signals"] = wcs.strip().lower() in ("true", "1", "yes", "y", "on")
    return out


# ─── quant_bot args 검증 (v3.22) ─────────────────────


def _validate_quant_args(args: dict) -> dict | None:
    """라우터가 채운 quant_bot 인자를 검증·정규화. 실패 시 None.

    v3.23 — recommend action 추가. top_n / market / phase_override 검증.
    """
    action = (args.get("action") or "").strip().lower()
    if action not in QUANT_ACTIONS:
        return None
    out: dict = {"action": action}

    months = args.get("months")
    if months is not None:
        try:
            mn = int(months)
            if 18 <= mn <= 60:
                out["months"] = mn
            elif mn < 18:
                out["months"] = 18
            elif mn > 60:
                out["months"] = 60
        except (TypeError, ValueError):
            pass

    if action == "recommend":
        top_n = args.get("top_n")
        if top_n is not None:
            try:
                tn = int(top_n)
                if 1 <= tn <= 30:
                    out["top_n"] = tn
                elif tn > 30:
                    out["top_n"] = 30
                # 1 미만은 무시 (디폴트 사용)
            except (TypeError, ValueError):
                pass
        market = (args.get("market") or "").strip().upper()
        if market in QUANT_MARKETS:
            out["market"] = market
        po = (args.get("phase_override") or "").strip()
        if po:
            for p in QUANT_PHASES:
                if po.lower() == p.lower():
                    out["phase_override"] = p
                    break
    return out


# ─── inbox_bot args 검증 ─────────────────────────────


def _validate_inbox_args(args: dict) -> dict | None:
    """라우터가 채운 inbox_bot 인자를 검증·정규화. 실패 시 None.

    content 최소 길이 10자 — 너무 짧은 입력은 메모할 가치 없거나 라우터 오분류
    가능성이 큼 → knowledge_bot으로 fallback.
    """
    content = (args.get("content") or "").strip()
    if len(content) < 10:
        return None
    out: dict = {"content": content}
    hint = (args.get("hint") or "").strip()
    if hint:
        out["hint"] = hint
    return out


# ─── coding_bot args 검증 ────────────────────────────


def _validate_coding_args(args: dict) -> dict | None:
    """라우터가 채운 coding_bot 인자를 검증·정규화. 실패 시 None."""
    action = (args.get("action") or "").strip().lower()
    if action not in CODING_ACTIONS:
        return None
    content = (args.get("content") or "").strip()
    if not content:
        return None

    out: dict = {"action": action, "content": content}

    language = (args.get("language") or "").strip().lower()
    if language:
        out["language"] = language

    files = args.get("files")
    if isinstance(files, list) and files:
        clean = []
        for f in files:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip() or "untitled"
            text = str(f.get("text") or "")
            if text.strip():
                clean.append({"name": name, "text": text})
        if clean:
            out["files"] = clean

    return out


# ─── JSON 파싱 헬퍼 ──────────────────────────────────


def _parse_json(text: str) -> dict | None:
    """LLM 출력에서 JSON 추출. 1) 직접 / 2) ```json``` 블록 / 3) 첫 { 부터 마지막 }"""
    if not text:
        return None

    # 1. 그대로 JSON?
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. ```json ... ``` 코드블록?
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. 처음 { 부터 마지막 } 까지
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


# ─── CLI 테스트 ──────────────────────────────────────


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용: python router.py '질문'")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    q = sys.argv[1]
    action = route(q)
    print(json.dumps(action, ensure_ascii=False, indent=2))
