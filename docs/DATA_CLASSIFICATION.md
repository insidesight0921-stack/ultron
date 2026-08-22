# Phase 1 데이터 분류 및 경계 설계

> 기준일: 2026-08-22  
> 근거: `local-ai-agent-plan.md` §1.5  
> 원칙: 로직은 공유 가능하지만, 그 로직을 개인 데이터에 적용한 결과는 Private이다.

## 1. 분류 정책

| 등급 | 의미 | 외부 LLM | MCP | Git |
|---|---|---:|---:|---:|
| **P0 Secret** | 토큰·API 키·계좌 인증정보 | 금지 | 금지 | 금지 |
| **P1 Private** | 개인 투자·일정·관심·원칙·성과·판단 이력 | 금지 | 금지 | 공개 저장소 금지 |
| **S Shareable** | 공개 원천 데이터와 일반화된 계산 로직 | 허용 | 명시적 allowlist만 허용 | 허용 |
| **M Mixed** | 공개 원천에 개인 설정·판단이 결합된 결과 | Private로 취급 | 금지 | 공개 저장소 금지 |

판정이 불분명하면 Private로 처리한다. MCP는 `Shareable` 표시만으로 자동 노출하지
않고, 별도의 도구 allowlist에 등록된 경우에만 접근시킨다.

## 2. 현재 데이터 자산 전수 분류

### P0 Secret

| 자산 | 현재 위치 | 생성·소비 주체 | 조치 |
|---|---|---|---|
| 텔레그램 토큰·허용 사용자 ID | `.env` | `telegram_bot.py`, `weekly_kium_scan.py` | 로컬 전용, 파일 모드 600 유지 |
| DART/ECOS/FRED/외부 LLM 키 | `.env` | finance/quant/coding 계열 | 로컬 전용, 로그·오류 응답에 값 출력 금지 |
| 향후 KIS 키·계좌번호 | `.env` | KIS 연동 전 | Private DB에도 원문 저장 금지 |

현재 Git의 추적 파일과 전체 커밋 경로에서 `.env`, DB, 로그가 들어간 흔적은 발견되지
않았다. 현재 로그에서도 일반적인 토큰·개인키 패턴은 발견되지 않았다.

### P1 Private

| 자산 | 현재 위치·스키마 | 이유 | 소비 주체 |
|---|---|---|---|
| 모의투자 | `data/paper.db`: portfolios, slots, positions, trades, ipo_records | 자산 규모·포지션·155건 거래 이력 | paper UI, Telegram, analytics/backtest |
| 일정 | `data/schedule.db`: events, event_pre_notifications | 일정 제목·메모·chat_id | Telegram 일정봇 |
| 관심종목 | `data/private.db`: watchlist | 개인 관심사 | watchlist bot |
| 자연어 자동작업 | `data/action_schedules.json` | 개인 루틴·발송 이력 | action scheduler |
| 개인 RAG 원본 | `obsidian-vault/wiki`, `raw` | 투자 원칙·판단·메모 | local Gemma, indexer |
| 개인 RAG 파생 | `data/lancedb` | Private 원문의 청크·임베딩 | knowledge/invest/finance bot |
| 성과·판단 상태 | `perf_history.json`, `phase_by_month.json`, `intraday_peaks.json` | 성과·국면 판단·현재 포지션 파생 | analytics/monitor |
| 발송·행동 이력 | `*_last.json`, `signal_last.json`, `news_seen.json` | chat별 발송 및 관심 행동 이력 | Telegram jobs |
| 정제 이력 | `data/raw_processed.json` | 개인 노트 경로·처리 결과 | refine/watch raw |
| 로그·리포트 | `data/logs`, `data/*.log`, `data/reports` | 사용자 요청·종목·오류·운영 메타 포함 가능 | 운영 진단 |

Vault 전체는 현재 하나의 Private 지식창고로 취급한다. 공개 가능한 팩터 설명이 섞여
있더라도 원칙·자산배분·개인 메모와 같은 인덱스에 들어가므로, 원본이나 LanceDB를
부분 필터링해 MCP로 직접 노출하지 않는다.

### S Shareable

| 자산 | 현재 위치 | 근거 | 향후 MCP 후보 |
|---|---|---|---|
| 종목·법인 마스터 | `corp_codes.json`, `ticker_map_*.json`, `etf_map_*.json` | 공개 거래소·DART 데이터 | instrument lookup |
| 시장 universe | `universe_*.json` | 공개 종목 구성 | list instruments |
| OHLCV | `data/cache/ohlcv/*.json` | 공개 시세 | get price/history |
| DART/KIND 원천·검증 샘플 | `dart_finance_validation.csv`, `ipo_samples/*`, `dart_metrics_cache.json` | 공개 공시·공개 웹 | get fundamentals/IPO facts |
| 경제지표 원천 | 런타임 ECOS/FRED 응답 | 공개 통계 | get macro indicator |
| 순수 계산 로직 | RSI/MACD, 팩터 계산, 국면 분류, 백테스트 엔진 | 일반화 가능 | calc indicator/factor/backtest |

현재 Git에서 추적되는 `data/cache/corp_codes.json`과
`data/dart_finance_validation.csv`는 공개 원천 데이터로 분류한다. 새 검증 데이터가
사람의 개인 라벨이나 개인 판단을 포함하면 Mixed로 재분류해야 한다.

### M Mixed — Private로 처리

| 자산·기능 | 혼합 요소 | 판정 |
|---|---|---|
| 콴텍/키움 추천 결과 | 공개 시세 + 개인 팩터 가중·선정 기준 | Private |
| 국면 판단 결과 | 공개 거시지표 + 개인 모델·판단 시점 | Private |
| signal bot 결과 | 공개 가격 + 개인 포트폴리오·비중 | Private |
| 백테스트 결과 | 공개 가격 + 개인 전략·파라미터 | Private |
| 현재 source repo | 일반 로직 + 개인 비중·손절 기본값 | 런타임 데이터 없이 제한적 공유 가능 |

외부 LLM에 코드를 보낼 때 `.env`, `data/`, vault, 실행 로그를 첨부하지 않는다. 개인
파라미터가 포함된 파일은 필요한 함수만 최소 범위로 제공하거나 값을 마스킹한다.

## 3. 현재 접근 경로

| 진입점 | 접근 데이터 | 현재 경계 |
|---|---|---|
| Telegram bot | watchlist, 일정, paper, vault RAG | user_id 화이트리스트 + local Gemma |
| Paper UI 8080 | paper DB 조회 및 buy/sell 쓰기 | 인증은 없지만 `127.0.0.1`에만 바인딩 |
| agent bot `read_file` | 프로젝트 scripts/docs/README와 vault wiki의 허용 텍스트 | 디렉터리·확장자 allowlist, 숨김 파일·DB 차단 |
| coding bot accurate mode | Anthropic API | 사용자 프롬프트가 외부 전송됨. Private 데이터 입력 금지 |
| LanceDB RAG | vault 전체 | local-only, MCP 없음 |
| MCP | 없음 | 아직 구현하지 않음 |

## 4. 목표 물리 경계와 DB 스키마

Phase 1에서는 아래 경계를 확정한다. 실제 경로 이동은 서비스 중단과 데이터 마이그레이션이
필요하므로 별도 승인 후 수행한다.

```text
data/
├── private/                  # mode 700, MCP/API shareable router 접근 금지
│   ├── assistant.db          # watchlist + 일정 + 자동작업
│   ├── paper.db              # 포트폴리오·포지션·거래·IPO 성과
│   ├── rag/                  # vault 파생 LanceDB
│   ├── state/                # 발송·성과·국면·정제 상태
│   └── logs/                 # private 운영 로그
└── shareable/                # 공개 원천과 재생성 가능한 캐시만
    ├── market.db             # 종목·시세·재무·거시 관측치
    ├── cache/
    └── samples/
```

### `private/assistant.db`

- `watchlist(ticker PK, name, source, created_at)`
- `events(id PK, title, when_at, notes, chat_id, recurrence, notification state)`
- `event_pre_notifications(event_id, minutes_before, notified)`
- `action_schedules(id, action, cadence, enabled, last_fired)`

일정의 `chat_id`와 자동작업은 Private이다. 향후 `schedule.db`와 JSON 자동작업을 이
스키마로 합치되, 기존 행을 검증한 다음 원본을 백업하고 전환한다.

### `private/paper.db`

현재 `portfolios`, `slots`, `positions`, `trades`, `ipo_records` 스키마를 유지한다.
다른 계층으로 복제하거나 집계 결과를 Shareable DB에 쓰지 않는다.

### `shareable/market.db`

- `instrument_master(ticker, name, market, corp_code, valid_from, valid_to, source)`
- `market_bar(ticker, trading_date, open, high, low, close, volume, source, fetched_at)`
- `fundamental_fact(ticker, metric, value, unit, period_end, report_date, rcept_dt, source)`
- `macro_observation(series_id, observed_at, value, vintage_at, source)`
- `factor_definition(key, version, formula, description)`

Point-in-Time 검증을 위해 `report_date/rcept_dt/vintage_at`을 보존한다. 개인 포트폴리오,
선정 여부, 점수 결과, 팩터 가중은 이 DB에 넣지 않는다.

## 5. MCP 노출 정책 초안

허용 후보:

- `get_instrument`, `get_price`, `get_market_history`
- `get_public_fundamentals`, `get_macro_indicator`
- `calc_indicator`, `calc_factor`, `run_backtest`

항상 금지:

- watchlist, 일정, chat_id
- 포트폴리오·포지션·거래·수익률
- vault 검색과 LanceDB 원문·청크
- 국면 판단 결과·추천 종목·발송 이력
- `.env`, 로그, 파일 읽기 도구

MCP 구현 시 데이터 경로를 인자로 받지 않고, 서버 내부의 Shareable 저장소만 고정 참조한다.
요청자가 임의 파일 경로나 SQL을 넘길 수 있는 범용 도구는 만들지 않는다.

## 6. 발견된 위험과 조치 순서

| 우선순위 | 상태 | 위험 | 제안 조치 |
|---|---|---|---|
| P0 | ✅ | Paper UI 외부 인터페이스 노출 | 자동 실행을 `127.0.0.1:8080`으로 제한. 원격 필요 시 Tailscale IP + 인증 적용 |
| P0 | ✅ | agent bot의 광범위한 파일 읽기 | scripts/docs/README/wiki와 텍스트 확장자 allowlist 적용, `.env`·DB·로그·숨김 경로 차단 |
| P1 | ✅ | Private DB·RAG·상태 파일 권한 | 파일 600, 디렉터리 700 적용. launchd 서비스 기본 `umask 077` |
| P1 | ✅ | Private SQLite 손상·오삭제 복구 | FileVault 로컬 경로에 주간 online backup, integrity check와 메모리 복구 검증 적용 |
| P1 | ⚠️ | 같은 디스크에만 백업 | Time Machine 대상 없음. 암호화된 외부·원격 사본 추가 필요 |
| P1 | ⚠️ | Private/Shareable 파일이 같은 `data/` 아래 혼재 | 위 목표 디렉터리로 단계적 이동, 호환 경로 기간 운영 |
| P2 | ⚠️ | SQLite SHM로 확인된 고아 `.fuse_hidden*` 25개(800KB) | 서비스 미사용 확인 후 승인받아 삭제 |
| P2 | ⚠️ | 삭제된 web UI의 로그 약 847KB 잔존 | 보존 기간 확인 후 승인받아 삭제 |

## 7. Phase 1 완료 판정

- [x] 실제 데이터 자산 전수 목록화
- [x] Private/Shareable/Mixed 판정
- [x] MCP allow/deny 경계 초안
- [x] Private/Shareable 목표 DB 스키마 확정
- [x] P0 노출 경로 2건 차단
- [x] Private 파일 권한 정책 적용
- [x] 로컬 SQLite 백업·복구 절차 구현 및 검증
- [x] legacy/private-v1 경로 호환 계층 적용
- [ ] 물리 경로 마이그레이션

분류·스키마 설계, P0 차단, 로컬 권한·DB 백업과 경로 호환 계층은 완료됐다. 현재 활성 레이아웃은 `legacy`이며 실제 데이터는 이동하지 않았다. `docs/STORAGE_MIGRATION.md`의 복사·검증·전환까지 끝나야 Phase 2 서버화로 넘어간다. 같은 디스크 장애 대비 사본은 별도 운영 과제로 남는다.

## 8. Private 보호 운영

- 권한 적용: `python scripts/private_data_security.py secure`
- 즉시 백업: `python scripts/private_data_security.py backup`
- 권한 + 백업: `python scripts/private_data_security.py all`
- 기본 백업 위치: `~/울트론/private-backups/ai-agent/<timestamp>/`
- 자동 실행: launchd `com.hyunjun.ai-agent.private-backup`, 매주 일요일 03:30
- 대상 DB: `paper.db`, `schedule.db`, `private.db`
- 검증: 각 사본에 `PRAGMA integrity_check`를 실행하고 메모리 DB로 복구한 뒤 스키마와 테이블 행 수를 비교한다.
- 보존: 기존 스냅샷을 자동 삭제하지 않는다.

2026-08-22 첫 스냅샷은 세 DB 모두 `integrity=ok`, `restore_verified=true`를 통과했다. FileVault는 켜져 있으나 Time Machine 대상은 설정되어 있지 않다.
