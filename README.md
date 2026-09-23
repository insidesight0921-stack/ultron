# Ultron — 로컬 LLM 멀티에이전트 개인 투자·지식 비서

**인터넷 없이 Mac 한 대에서 도는, 실주문 없는 투자 리서치 + 개인 지식 관리 시스템**
Gemma 4 (26B MoE 라우터 · 31B Dense 답변) on Apple Silicon · Telegram 인터페이스 · LanceDB RAG · 페이퍼 트레이딩 검증 루프 · Private/Shareable 데이터 경계 · MCP 서버

> *A local-first multi-agent assistant for personal investing and knowledge management. A 26B MoE model routes Telegram messages to ~15 specialist bots (RAG, macro indicators, factor scoring, momentum scan, IPO scoring, paper trading, news, scheduling). No real orders are ever placed; every signal goes to a human. Private data is physically separated from shareable market data, and only the shareable side is exposed through a read-only MCP server.*

> ⚠️ 이 시스템은 매매 신호를 **제안**할 뿐 실주문을 하지 않습니다. 모든 수치·전략은 개인 검증용이며 투자 권유가 아닙니다.

**기간** 2026-04 ~ 진행 중 · **규모** Python 약 9만 줄, 스크립트 130개, 테스트 파일 140개 · **1인 개발**

---

## 1. 무엇을 푸는가

| 반복되던 문제 | 이 프로젝트의 답 |
|---|---|
| 투자 메모·원칙이 Obsidian에 흩어져 있어 판단 때마다 못 찾음 | vault를 LanceDB로 색인해 **원칙 대조 RAG** — "이 종목 사도 돼?"에 내 규칙을 근거로 답함 |
| 지표·시세·공시를 매번 손으로 조회 | ECOS·FRED·DART·KRX를 봇이 수집, 텔레그램 한 줄로 조회 |
| "이 전략이 진짜 통하나"를 감으로 판단 | **페이퍼 트레이딩 6개월 검증** → 거래 이력 통계 → 규칙 후보 → 전방 검증 |
| 개인 포트폴리오·일정이 외부 LLM·도구에 새어 나갈 위험 | 데이터를 **P0/P1/S/M 4등급으로 분류**, Private와 Shareable을 디렉터리·프로세스·포트 단위로 물리 분리 |
| 외부 AI 도구(Codex 등)에 시장 데이터만 주고 싶음 | Shareable API만 감싼 **읽기 전용 MCP 서버** (6개 도구 allowlist, fail-closed) |
| 봇이 늘 때마다 텔레그램 핸들러를 새로 짬 | 라우터가 JSON으로 도구를 고르고, 다단계 작업은 ReAct 에이전트가 조합 |

## 2. 구조

```
Telegram (user_id 화이트리스트)
  └─ router.py          Gemma 4 26B MoE — 사용자 입력 → {tool, args} JSON 한 번 분기
       ├─ knowledge_bot   vault RAG(LanceDB) + Gemma 4 31B 답변 · 미스 시 research_bot이 웹 검색→wiki 저장
       ├─ finance_bot     ECOS(한국은행)·FRED 지표 + 투자 원칙 대조
       ├─ invest_bot      pykrx 차트 지표 + wiki 매매 규칙 대조
       ├─ quant_bot       거시 국면 분류(Recovery/Expansion/Slowdown/…) + 5팩터 스코어링 추천
       ├─ kium_bot        KOSPI200/KOSDAQ150 12-1 모멘텀 스캐너
       ├─ ipo_bot         DART 수요예측 파싱 → 공모주 매력지수(5요소 100점)
       ├─ signal_bot      자산배분 종목 1시간봉 보조지표 → 매매 신호 알림(실주문 없음)
       ├─ news_bot        RSS IT/AI 뉴스 다이제스트 (정시 + 온디맨드)
       ├─ schedule_bot · action_scheduler   일정 리마인더 · "매일 8시 뉴스 보내" 같은 자연어 반복 작업
       ├─ inbox_bot · refine_raw · learning_bot   메모 → raw/ → Gemma가 wiki로 정제 (NEW/MERGE/SKIP 판단)
       ├─ coding_bot      로컬 Qwen2.5-Coder 32B ↔ Claude API 하이브리드 (키 없으면 자동 로컬 폴백)
       └─ agent_bot       위 도구를 ReAct 루프로 연쇄 호출하는 다단계 오케스트레이터

Paper Trading (paper_db · paper_ui :8080)   슬롯 분리 모델(콴텍/키움/IPO/마이퀀트) 가상 매매 · 자산곡선 · 손익 통계
Backtests (entry/exit/weight_rule_backtest · phase_history · phase_factor_eval)

데이터 경계
  data/private/    assistant.db(일정·관심종목) · paper.db · rag/ · state/ · logs/   ← mode 700, MCP 접근 금지
  data/shareable/  market.db · cache/ · samples/  (공개 시세·공시·지표만, 매일 16:20 launchd 갱신)

서비스 (launchd, 모두 127.0.0.1 바인딩)
  :8090 Shareable Data API  ──stdio──▶ shareable_mcp.py (MCP host: Codex 등)
  :8091 Private Data API    Bearer 인증 · Telegram/Paper UI만 사용
```

## 3. 설계 판단

**① 실주문은 코드에 없다.** 모든 봇은 신호·추천·알림까지만 하고 주문 API를 호출하지 않는다. 6개월 페이퍼 트레이딩으로 전략이 검증될 때까지 실계좌 연동 코드를 아예 넣지 않는 것이 가장 확실한 안전장치라고 판단했다.

**② 데이터는 로직보다 먼저 분류한다.** "로직은 공유 가능하지만, 그 로직을 개인 데이터에 적용한 결과는 Private"이라는 원칙으로 자산 전수를 P0 Secret / P1 Private / S Shareable / M Mixed로 분류했다(`docs/DATA_CLASSIFICATION.md`). 판정이 애매하면 Private. 이 분류가 디렉터리 권한, API 포트, MCP allowlist까지 그대로 내려간다.

**③ MCP는 fail-closed.** `shareable_mcp.py`는 파일·DB를 직접 읽지 않고 8090 API만 호출한다. 도구 자동 탐색 없이 코드의 allowlist(6개)와 실제 등록 집합이 다르면 시작을 거부하고, API가 죽으면 오류를 반환한다. API 응답에 새 필드가 생겨도 MCP 출력은 도구별 공개 필드만 다시 선별한다. 공식 MCP Inspector와 실제 Codex 호출로 tool list가 정확히 6개인지 실측했다.

**④ 한 번 분기는 라우터, 여러 단계는 에이전트.** 라우터(26B MoE)는 JSON 한 번으로 도구 하나를 고른다 — 빠르고 예측 가능. 도구를 조합해야 하는 요청만 `agent_bot`의 ReAct 루프로 보낸다. 임의 셸 실행·파일 쓰기는 에이전트 v1에서 제외했다.

**⑤ 외부 LLM은 옵션, 로컬이 기본.** 라우팅·RAG·정제는 전부 로컬 Gemma. 코딩봇만 정확도가 필요할 때 Claude API를 쓰고, 키가 없으면 자동으로 로컬 Qwen으로 내려간다. Private 데이터는 외부 LLM에 절대 입력하지 않는다는 규칙과 맞물린다.

**⑥ 과거 판정에 미래 정보를 섞지 않는다.** 거시 국면 이력(`phase_history.py`)은 t월의 국면을 t월까지 발표된 값으로만 되감아 계산한다. 전체 시계열로 한 번에 계산해 자르면 정확도가 가짜로 올라간다.

## 4. 페이퍼 트레이딩에서 배운 것 (버그 회고)

이 프로젝트의 가장 큰 산출물은 코드가 아니라 **"좋게 나오는 오류는 잡히지 않는다"**는 교훈이다.

- **슬롯 비중 합계 110%.** 마이퀀트 슬롯(10%)이 기존 90% 위에 얹혔는데 아무도 합계를 안 봤다. 자산곡선이 시드 1억이 아닌 1.1억에서 시작했고, 초과수익 +2.09%p가 실제로는 **−0.12%p** — 전략 전환 판정이 뒤집혔다. 개별 값 검증(0~1 사이)은 전부 통과했기 때문에 놓쳤다. → `slot_allocation.py`가 합계를 자동 검증한다.
- **같은 성격의 반복.** 샤프비율 √252 오연환산, 볼린저 밴드 하단을 확정가로 오인 — 셋 다 성적을 **유리하게** 틀리는 오류였다. 나쁘게 틀리면 눈에 띄어 금방 잡힌다.
- **"목요일의 저주" 기각.** 목요일 매수 16건 승률 6%로 보였지만 전부 두 날의 일괄 매수였다. 요일이 아니라 "손절 후 30일 내 같은 종목 재진입"과 "Slowdown 국면 신규 진입"이 진짜 변수였다(`docs/엣지분석_2026-07-09.md`). 두 규칙은 EDGE_RULES로 등록해 전방 검증 중이다.
- **표본이 작으면 크기 대신 부호만 본다.** 국면×팩터 효과 검정은 에피소드 18개로 최소 검출 차이가 월 5%p — 현실 효과(0.3~0.5%p)의 10배라 평균 차이는 잴 수 없다고 결론내고 부호 일치율만 본다(`phase_factor_eval.py`).

## 5. 실행 환경

- macOS Apple Silicon 48GB · Python 3.11+ · Ollama (`gemma4:26b-moe`, `gemma4:31b`, `qwen2.5-coder:32b`)
- 별도 Obsidian vault(개인 지식, 비공개 repo)
- 외부 API 키: Telegram · DART · ECOS · FRED · (선택) Anthropic

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # 키 입력, 파일 모드 600
python scripts/check_env.py     # 키·모델·경로 점검

python scripts/index_wiki.py            # vault → LanceDB 색인
python scripts/telegram_bot.py          # 봇 시작
python scripts/paper_ui.py              # http://127.0.0.1:8080
bash scripts/agent_services.sh          # 8090/8091 launchd 등록 (상시 운영용)
```

이 저장소는 개인 운영 환경에 맞춰져 있어 vault와 Private DB 없이는 일부 봇이 축소 동작한다. 공개 시장 데이터 봇(quant/kium/ipo)과 Shareable API·MCP는 vault 없이 동작한다.

## 6. 검증

```bash
pytest            # 네트워크·LLM 없이 hermetic (monkeypatch)
```

- 테스트 파일 140개. 외부 HTTP·LLM 호출은 모듈 함수로 분리해 monkeypatch로 가짜화하므로 키 없이 돈다.
- 페이퍼/Private DB는 테스트 전후 integrity가 변하지 않는 것을 감사 스크립트로 확인한다.
- MCP: `scripts/smoke_shareable_mcp.py`(stdio 실측), `scripts/audit_shareable_mcp.py`(host 설정·allowlist·읽기 전용 annotation 감사).

## 7. 문서

| 문서 | 내용 |
|---|---|
| `docs/DATA_CLASSIFICATION.md` | 4등급 데이터 분류 정책과 자산 전수 분류, 물리 경계·DB 스키마 |
| `docs/API_SERVERIZATION.md` | 직접 파일 접근 → API 계약 전환 순서, 보안 테스트 기준 |
| `docs/PRIVATE_API_CONTRACT.md` | Private API 인증·쓰기 계약(멱등키·승인·감사 로그) |
| `docs/MCP_SERVERIZATION.md` | MCP 도구 allowlist, fail-closed 경계, Inspector·Codex 실측 |
| `docs/STORAGE_MIGRATION.md` · `*_CUTOVER_RUNBOOK.md` | 비파괴 마이그레이션과 롤백 절차 |
| `docs/엣지분석_2026-07-09.md` | 페이퍼 트레이딩 42건 통계 → 규칙 후보 도출 |
| `docs/internal/` | 개인 작업 상태·A/B 실측 원본 (프로젝트 이해에는 불필요) |

## 8. 현재 상태와 한계

- 완료: 모델 실측 → RAG → Telegram → 라우터 → Private/Shareable 분리 → Data API → MCP (Phase 1~3)
- 진행 중: 페이퍼 트레이딩 전방 검증, IPO 배점 개편, 손절 상관분석
- 한계: 모멘텀 백테스트는 pykrx가 현재 상장 종목만 반환해 생존 편향으로 1~2%p 과대 추정 · VKOSPI는 검증된 무인증 소스가 없어 수집 안 함 · 페이퍼 표본이 작아 규칙은 전부 가설 단계
- 실계좌(KIS) 연동은 페이퍼 검증 완료 전까지 하지 않는다.

## 9. 보안·면책

- 시크릿은 `.env`에서만 로드. Git 전체 커밋 이력에 `.env`·DB·로그가 들어간 흔적 없음을 확인함.
- 텔레그램은 user_id 화이트리스트, 모든 서버는 loopback 바인딩.
- 투자 판단은 전적으로 사용자 책임이며, 이 저장소의 신호·통계는 투자 권유가 아니다.
