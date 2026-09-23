# ✅ [완료] system_info (봇 자기/시스템 인식)
> 2026-06-17 구현 완료 — router v3.45 · research v1.1 · telegram v3.45.
> scripts/system_info.py + tests/test_system_info.py(41). 회귀 672 PASS. 상세는 NEXT_SESSION.md.
> (아래는 작업 당시 계획 원문 — 참고용)

# 🚀 다음 작업 — system_info (봇 자기/시스템 인식)

> 새 대화 시작 시 이 파일을 먼저 읽으세요. 이 한 파일로 바로 작업 착수 가능하게 정리했습니다.
> 작성: 2026-06-08 세션 종료 시점 / 대상 레포: `~/울트론/ai-agent`

## 0. 한 줄 목표
봇이 **자기 자신·자기 상태에 대한 질문**("paper 주소 뭐야", "리밸런싱 됐어?", "무슨 봇 돌고 있어?", "마지막 신호 언제 보냈어?")에
wiki RAG/웹으로 새지 않고 **실제 시스템 상태에서 결정론적으로** 답하게 한다.

## 1. 왜 (이번 세션 실측 실패 사례)
- "내 paper trading 로컬 사이트 주소 줘봐" → wiki RAG로 가서 "노트 없음"으로 실패. (정답: http://localhost:8080)
- "리밸런싱 완료된거야?" → wiki 없음 → **웹 검색**까지 가서 "못 찾음". (정답: data/cache/quant_rebalance_last.json·action_scheduler last_fired에 있음)
- 근본 원인: 라우팅에 **"내 시스템/내 상태"** 갈래가 없음. 메타 질문이 투자 RAG·웹으로 오발동.

## 2. 만들 것 (4개 개선이 이걸로 한 번에 해결)
### (A) `scripts/system_info.py` 신규 — 결정론 자기인식 응답기
질문 유형별로 실제 상태를 읽어 문자열로 답하는 순수/저의존 함수들:
- **접속 정보**: Paper http://localhost:8080, 로그 경로. (telegram_bot 상수/포트에서)
- **서비스 상태**: `agent_services.sh status` 파싱 또는 launchd 서비스(watch-raw/telegram/paper) 안내.
- **등록된 봇/도구 목록**: knowledge/schedule/finance/invest/kium/quant/ipo/news/action_schedule/agent.
- **자동작업 상태**: action_scheduler.load_schedules() + last_fired, 그리고 플래그 파일들
  (quant_rebalance_last.json / kium_weekly_last.json / ipo_weekly_last.json / signal_last.json) 읽어 "마지막 실행 시각".
- **리밸런싱/신호/스캔 실행 여부**: 위 플래그·last_fired로 "○월○일에 실행됨 / 아직 안 됨" 답.

### (B) 라우터 결정론 감지 `_detect_system_info(query)` (router.py)
- 단서: `주소|포트|로컬\s*사이트|사이트 주소|paper|서비스 상태|무슨 봇|어떤 봇|봇 목록|돌고 있|실행 중|마지막.*(실행|신호|스캔)|리밸런싱.*(됐|완료|했)`
- 매칭 시 `{"tool":"system_info","args":{"topic":...}}` short-circuit (LLM 미호출). knowledge/news/research보다 **먼저**.
- KNOWN_TOOLS에 "system_info" 추가 + spec 1줄 + validator.

### (C) research 웹폴백 가드 (telegram knowledge 분기)
- `rag_is_weak`라도 **자기참조/개인 질문이면 웹 검색 금지**.
- `research_bot`에 `is_self_referential(query)` 추가: `내|나의|우리|제|시스템|봇|리밸런싱|포지션|슬롯|주소|포트|자동작업` 포함 시 True.
- knowledge 분기: `if rag_is_weak and not is_self_referential and not system_info매칭 → research`.

### (D) 텔레그램 분기 + 시드 wiki(선택)
- `elif tool == "system_info":` → system_info 함수 호출해 answer.
- (선택) `wiki/시스템/접속정보.md` 노트 시드해서 RAG로도 잡히게.

## 3. 손댈 파일
- 신규: `scripts/system_info.py`, `tests/test_system_info.py`
- 수정: `scripts/router.py`(감지+도구+validator+spec), `scripts/telegram_bot.py`(분기+가드), `scripts/research_bot.py`(is_self_referential)
- 프롬프트 한도: 현재 ~12559자 / 테스트 한도 13000(test_coding_bot). 여유 있으나 spec은 1~2줄로 간결히.

## 4. 테스트 계획 (sandbox는 망·lancedb·telegram 불가 → mock/순수 함수 위주)
- system_info: 각 topic 함수가 포트/플래그 파일(tmp)에서 올바른 문자열 만드는지.
- router: `_detect_system_info` 긍정/부정 + short-circuit가 urlopen 없이 동작.
- research: `is_self_referential` 긍정/부정.
- 회귀: 기존 465 PASS 유지.

## 5. 착수 순서(권장)
1. 이 파일 + `docs/NEXT_SESSION.md` 상단 읽기
2. system_info.py 함수부터(플래그 경로는 telegram_bot 상수 참고: REBALANCE_FLAG_DIR=PROJECT/data/cache)
3. 테스트 → 라우터 감지 → 텔레그램 분기 → research 가드 순
4. 전체 회귀 + 사용자에게 "봇 재시작 후 'paper 주소 뭐야'/'리밸런싱 됐어?' 검증" 안내

## 6. 사용자 미완 액션(이전 세션에서 넘어옴)
- 봇 재시작해서 이번 세션 변경(action_scheduler·research_bot·news run_daily 폐지 등) 반영 확인.
- `data/cache/news_digest_last.json` 삭제(옛 플래그). "자동작업 목록"에 7종 뜨는지 확인.
- pip: `feedparser`, `finance-datareader`, `beautifulsoup4`(없으면).
