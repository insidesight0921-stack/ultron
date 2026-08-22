# Private / Shareable 물리 분리 실행서

> 상태: 1차 호환 코드 완료, 실제 레이아웃은 `legacy`  
> 원칙: 복사 → 검증 → 전환. 기존 파일 삭제는 별도 승인 전까지 금지한다.

## 1. 레이아웃 선택 방식

모든 런타임 경로는 `scripts/storage_paths.py`에서 결정한다.

1. `AI_AGENT_STORAGE_LAYOUT` 환경변수가 있으면 우선한다.
2. 없으면 ignored 로컬 파일 `data/storage-layout.json`을 읽는다.
3. 파일이 없거나 값이 잘못되면 안전하게 `legacy`를 선택한다.

지원 값은 `legacy`, `private-v1` 두 개뿐이다. 현재 설정 파일이 없으므로 기존
`data/` 경로를 그대로 사용한다. 코드 배포만으로 데이터 위치가 바뀌지 않는다.

## 2. 목표 매핑

| 자산 | legacy | private-v1 |
|---|---|---|
| Paper DB | `data/paper.db` | `data/private/paper.db` |
| 일정 + 관심종목 DB | `data/schedule.db`, `data/private.db` | `data/private/assistant.db` |
| 개인 RAG | `data/lancedb` | `data/private/rag` |
| 자동작업·성과·발송 상태 | `data/action_schedules.json`, `data/cache/*_last.json` 등 | `data/private/state` |
| 로그·리포트 | `data/logs`, `data/reports`, `data/watch_raw.log` | `data/private/logs`, `data/private/reports` |
| 공개 시세·종목·DART 캐시 | `data/cache` | `data/shareable/cache` |
| 공개 IPO/DART 검증 샘플 | `data/ipo_samples`, `data/*_validation.csv` | `data/shareable/samples` |

`assistant.db`에는 `events`, `event_pre_notifications`, `watchlist` 테이블을 합친다.
자연어 자동작업은 1차에는 `private/state/action_schedules.json`을 유지하고, DB 테이블
전환은 별도 스키마 변경으로 다룬다.

## 3. 2차 복사·전환 절차

1. 최신 Private SQLite 온라인 백업을 만들고 메모리 복구를 검증한다.
2. AI-agent 서비스만 중지한다. Tapnow와 Ollama는 유지한다.
3. `data/private`, `data/shareable`을 각각 mode 700으로 생성한다.
4. `paper.db`는 SQLite online backup으로 새 위치에 복제한다.
5. `schedule.db`와 `private.db`를 새 `assistant.db`에 테이블 단위로 병합한다.
6. RAG·상태·로그·리포트·공개 캐시는 복사하고 파일 해시를 비교한다.
7. DB integrity, 스키마, 테이블 행 수와 RAG 22문서/125청크를 검증한다.
8. `data/storage-layout.json`에 `{"layout":"private-v1"}`을 mode 600으로 기록한다.
9. 서비스 설정을 다시 생성하고 AI-agent 서비스를 시작한다.
10. Paper UI, 텔레그램, watch_raw, 백업 스케줄과 새 로그 경로를 확인한다.

복사는 전용 마이그레이션 도구로만 수행한다. 범용 파일 이동이나 재귀 삭제 명령은
사용하지 않는다.

## 4. 전환 판정

- Paper UI가 `127.0.0.1:8080`에서 HTTP 200
- 텔레그램과 watch_raw 정상 기동
- `paper.db`: 기존 테이블별 행 수와 일치
- `assistant.db`: 일정·알림·관심종목 행 수와 일치
- RAG: Wiki 22개, 125청크, missing/stale/outdated 0
- 새 로그가 `data/private/logs`에만 생성
- 새 온라인 백업이 `paper.db`, `assistant.db` 모두 복구 검증 통과
- 전체 테스트 통과
- `data/shareable`에 개인 상태·추천 결과·chat_id·포트폴리오 데이터 없음

## 5. 롤백과 보존

전환 직후 문제가 생기면 레이아웃 설정을 `legacy`로 되돌리고 AI-agent 서비스만
재시작한다. 전환 후 새 DB에 쓰인 데이터가 있으면 역동기화 검증을 먼저 수행한다.

기존 DB·RAG·로그는 최소 한 번의 새 레이아웃 주간 백업 성공 전까지 보존한다. 이후에도
고아 `.fuse_hidden*`, Web UI 로그, legacy 사본 삭제는 각각 명시적 승인을 받아야 한다.

## 6. 1차 코드 적용 범위

- Private DB: `paper_db.py`, `schedule_bot.py`, `watchlist_store.py`
- RAG·상태: `ask.py`, `index_wiki.py`, `refine_raw.py`, `telegram_bot.py`, 분석·스케줄 모듈
- Shareable: quant/signal/invest/kium/IPO/DART 캐시·샘플 모듈
- 운영: `agent_services.sh`, `rotate_logs.sh`, `private_data_security.py`

경로를 직접 받는 테스트 API는 그대로 유지해 실제 DB와 테스트 DB의 격리를 보존한다.
