> ✅ **2026-08-22 현재**: private 관심종목 저장소 + Function Calling 라우팅 + 텔레그램 실행 경로 구현 완료.
> 자연어 예: "삼성전자 관심종목에 추가해줘", "관심종목 보여줘", "삼성전자 관심종목에서 빼줘".
> 데이터는 `data/private/assistant.db`에만 저장되며 git/MCP에 노출하지 않는다.
>
> ✅ **원칙 Wiki/RAG 정합성 점검 완료**: 현재 24개 Wiki가 134청크로 모두 인덱싱되며 삭제·누락·구버전 청크가 없다.
> ✅ **데이터 분류 Phase 1 완료**: `docs/DATA_CLASSIFICATION.md`의 분류·보호·물리 경계를 실제 `private-v1` 레이아웃에 적용했다.
> ✅ **P0 노출 경로 차단 완료**: Paper UI는 `127.0.0.1:8080`만 사용하고 agent bot 파일 읽기는 프로젝트 scripts/docs/README와 vault wiki의 허용 텍스트만 접근한다.
> ✅ **P1 로컬 보호 완료**: Private 파일 600/디렉터리 700, FileVault 로컬 SQLite 온라인 백업과 메모리 복구 검증, 매주 일요일 03:30 자동 실행을 적용했다.
> ✅ **물리 분리 전환 완료**: `storage_paths.py`가 선택한 활성 레이아웃은 `private-v1`이다. `paper.db`와 통합 `assistant.db`, RAG·상태·로그는 `data/private`, 공개 캐시·샘플은 `data/shareable`을 사용한다.
> ✅ **비파괴 전환 검증 완료**: 최신 legacy 백업 → AI-agent 서비스 중지 → 복사·DB 병합·해시 검증 → 레이아웃 활성화 → 서비스 재기동을 완료했다. legacy 원본은 삭제하지 않고 보존한다.
> ✅ **운영 검증**: Paper UI 8080 HTTP 200, Telegram/watch_raw 정상, Tapnow 8082·Ollama 11434 유지, DB 행 수 일치, 전환 후 2개 DB 복구 백업 성공, 전체 테스트 1,048개 통과.
> ✅ **역사적 Phase 2 착수 기준**: 데이터 접근 API의 private/shareable 경계를 먼저 고정하고 직접 파일·DB 접근을 작은 단위로 전환했다. Phase 2와 Phase 3 로컬 STDIO MVP는 아래 최신 항목처럼 완료됐으며, 외부 복구 사본은 별도 운영 과제다.
> ✅ **Phase 2 공개 수집·읽기 경계 완료**: `127.0.0.1:8090` Shareable API를 launchd로 운영하고 invest/quant/kium 읽기를 API 우선으로 전환했다. 시장지수·universe·OHLCV·ticker-map·fundamental·시가총액의 외부 수집과 Shareable 쓰기는 전용 `market_data_collector.py`가 소유한다. factor API와 비거래일 후퇴를 운영 적용했다.
> ✅ **Private API watchlist·일정 조회 운영 전환 완료**: 8091의 read-only 경계에서 watchlist와 chat별 일정 list/upcoming을 운영한다. Telegram 조회는 API 우선/DB 폴백이며 모든 쓰기와 일정 알림 스케줄러는 기존 직접 경로다. `writes_enabled=false`, 전체 테스트 1,163개 통과.
> ✅ **Private Paper portfolio/position 조회 운영 전환 완료**: 8091의 고정 read-only Paper 라우트와 Paper UI API 우선/DB 폴백을 적용했다. 운영 1/9건, 무인증 401, 안정화 뒤 DB 해시·논리 스냅샷 무변경, 전체 테스트 1,173개 통과. 모든 쓰기는 직접 경로로 보존한다.
> ✅ **Private Paper slots/trades 조회 운영 전환 완료**: 기본 대시보드의 슬롯 집계와 최근 거래를 8091 API 우선/DB 폴백으로 전환했다. 운영 slots 4 / trades 164, UI trades 100, 무인증 401, 조회 전후 DB 불변, 전체 테스트 1,182개 통과.
> ✅ **Private Paper IPO 조회 운영 전환 완료**: IPO records/stats를 8091 API 우선/DB 폴백으로 전환했다. 운영 0/0건 빈 목록, 무인증 401, 조회 전후 DB 불변, 전체 테스트 1,192개 통과. subscribe/close 쓰기는 직접 경로다.
> ✅ **Private Paper 성과 계산 조회 운영 전환 완료**: 공통 순수 FIFO 계산 계층을 두고 performance/myquant-tags를 8091 API 우선/DB 폴백으로 전환했다. 운영 성과 4행·태그 0건, 원시 거래 비노출, 무인증 401, DB 불변, 전체 테스트 1,199개 통과.
> ✅ **Private 쓰기 사전 계약 완료**: 실제 mutation 없이 9개 operation allowlist, 멱등키, UUID 요청·승인 ID, expected version, 승인 상태 머신, 비민감 감사 이벤트를 순수 검증 계층으로 고정했다. HTTP mutation은 0개이고 `writes_enabled=false`를 유지한다. 전체 테스트 1,218개 통과.
> ✅ **watchlist 격리 쓰기 저장 검증 완료**: 기본 쓰기 잠금과 명시 DB 경계를 둔 저장 계층을 추가했다. 임시 SQLite에서 pending→approved→applied 1회 add/remove, 동일 요청 replay, 멱등키·버전·승인 충돌, payload 비노출 감사를 검증했다. 운영 DB/API에는 연결하지 않았다. 전체 테스트 1,229개 통과.
> ✅ **watchlist 기본 비활성 HTTP 계약 완료**: writer를 명시 주입한 테스트 앱에만 intent/approve/reject/apply 네 POST를 등록했다. 인증·no-query·요청 크기·중복 JSON·필수 UUID/멱등 헤더·HTTP 충돌을 임시 DB에서 검증했다. 운영 CLI에는 writer/활성화 옵션이 없어 mutation route 0개와 `writes_enabled=false`를 유지한다. 전체 테스트 1,241개 통과.
> ✅ **watchlist 기본 비활성 client 완료**: `PrivateDataClient` 생성자에서 명시 활성화한 테스트 객체만 submit/approve/reject/apply를 호출한다. 임시 HTTP 앱에서 request+approval UUID 결합, replay, 401/409/413/422, 엄격한 응답 allowlist를 검증했다. 환경변수·Telegram·운영 DB 연결은 없다. 전체 테스트 1,250개 통과.
> ✅ **Telegram watchlist write identity 완료**: update/chat/user/message/action을 결정론적 request UUID·approval UUID·Idempotency-Key로 변환해 add/remove dispatch에 전달한다. 원문·종목·Telegram 숫자 ID는 identity에 남기지 않는다. `watchlist_bot`은 아직 기존 직접 쓰기를 사용하며 client/plist/운영 API 연결은 없다. 전체 테스트 1,267개 통과.
> ✅ **watchlist idempotent preflight 완료**: 테스트 주입 store/API/client에서 신규 요청은 현재 version, 기존 요청은 최초 expected version·state·완료 result를 payload 없이 복원한다. 식별자/operation 불일치는 409이며 한 SQLite snapshot으로 읽고 감사·관심종목을 바꾸지 않는다. 운영 기본 앱에는 경로가 없다. 전체 테스트 1,278개 통과.
> ✅ **watchlist 기본 비활성 consumer executor 완료**: executor+client 이중 활성화와 호출별 명시 승인을 요구하고, preflight→submit→approve→apply 및 pending/approved/applied 재개를 임시 HTTP/SQLite에서 검증했다. rejected/expired와 payload drift는 중단하며 API 실패 시 직접 DB 폴백이 없다. Telegram 운영 경로에는 executor를 주입하지 않았다. 전체 테스트 1,288개 통과.
> ✅ **Private 쓰기 readiness gate 완료**: API writer/client/Telegram executor 세 조건, 단일 `private-data-api` writer, private `assistant.db` 무결성, 최근 24시간 복구 검증 백업의 권한·해시·schema·행 수, 호출별 승인, direct DB 무폴백, rollback을 순수 보고서로 검사한다. 손상·위조·부분 활성화는 fail-closed이며 운영 설정·DB·서비스는 바꾸지 않았다. 전체 테스트 1,305개 통과.
> ✅ **Private 쓰기 공통 activation permit 완료**: readiness 전체 통과 뒤에만 DB/backup fingerprint-bound permit을 발급하고 write API 앱, client, executor가 모두 요구한다. 누락·직접 생성·일반 보고서 대체·DB scope·client/executor permit 불일치는 생성 단계에서 거부한다. 운영 CLI/Telegram/8091에는 발급·주입 경로가 없다. 전체 테스트 1,311개 통과.
> ✅ **Private 쓰기 cutover/rollback dry-run 완료**: 현재 write stack 비활성·단일 legacy writer와 목표 단일 API writer를 분리하고 cutover 6단계·rollback 5단계 순서를 고정했다. 자동 DB 복원·순서 drift·부분 활성화를 거부하고 결과에는 permit 객체나 실행 기능이 없다. 운영 read-only 결과는 `backup_fresh`, `rollback_verified` 미충족으로 ready=false이며 DB는 불변이다. 전체 테스트 1,323개 통과.
> ✅ **fresh backup/rollback rehearsal 완료**: `20260825T224422+0900`에 운영 paper/assistant DB를 온라인 백업·메모리 복구 검증했고 원본은 불변이었다. fresh assistant clone에서 API write→legacy writer 재개·API 결과 보존·별도 emergency restore 일치를 확인했다. 산출물 `/private/tmp/ultron-private-rollback-so8mv5vr`은 삭제하지 않았다. 재평가 dry-run은 ready=true이며 실제 활성화 승인은 아니다. 전체 테스트 1,328개 통과.
> ✅ **기본 비활성 atomic runtime bundle 완료**: 단일 600 private JSON과 bundle ID 없이는 API writer/client/Telegram executor 어느 것도 활성화되지 않는다. 고정 DB·승인 backup root·세 플래그·사용자 승인·무폴백·rollback·자동 복원 금지·permit fingerprint를 양 프로세스가 재검증한다. 현재 env/plist/file은 미설정이고 운영 mutation route/write table은 0개다. 전체 테스트 1,345개 통과.
> ✅ **비설치 activation candidate/runbook 완료**: 운영 root와 다른 빈 700 staging에만 600 candidate를 exclusive 생성하고 API/Telegram loader가 같은 bundle/permit fingerprint를 재검증한다. 운영 증거 candidate는 `/private/tmp/ultron-private-write-candidate-p46sn34t/`에 보존했지만 미설치다. 고정 파일·env/plist key는 없고 운영 DB는 불변이다. `docs/PRIVATE_WRITE_CUTOVER_RUNBOOK.md`에 승인 후 순서를 고정했다. 전체 테스트 1,352개 통과.
> ✅ **운영 watchlist write cutover 완료 (2026-08-26)**: fresh backup `20260826T005056+0900`·격리 rollback rehearsal 뒤 atomic bundle `528fcb275432…`를 600 private 파일과 `.env` 단일 키로 활성화했다. 8091은 `writes_enabled=true`와 5개 mutation route, Telegram은 동일 API executor로 운영하며 direct DB 쓰기 폴백이 없다. 무데이터 replay 뒤 실제 Telegram에서 `삼성전자(005930)` add/remove가 각각 pending→approved→applied로 완료됐다. 최종 watchlist 0건·version 2이며 실제 사용자 update의 양방향 경로까지 검증했다. macOS 한글 경로 NFC/NFD 차이를 고정 위치 비교에서 정규화했다.
> ✅ **일정 쓰기 순수 계약 완료**: `schedule.add/delete/complete`를 실제 라우터의 RRULE·다중 사전알림 payload와 일치시키고 chat scope hash를 intent fingerprint에 결합했다. repr/audit에는 chat ID·일정 본문을 남기지 않으며 아직 DB·HTTP·Telegram runtime에는 연결하지 않았다. 전체 테스트 1,374개 통과. 다음 구현 단위는 기본 비활성 일정 격리 저장소다.
> ✅ **기본 비활성 일정 격리 저장소 완료**: 공통 scope-bound 승인 상태 머신과 schedule SQL 적용부를 분리했다. 임시 DB에서 chat별 version, 승인 전 무변경, add/delete/complete, replay, stale 만료, reject, 다른 chat 접근 차단과 비민감 audit를 검증했다. 운영 DB의 `private_scoped_*` table은 0개이고 일정 5건·watchlist 0건/version 2를 유지한다. 전체 테스트 1,385개 통과. 다음 구현 단위는 writer를 명시 주입한 테스트 앱의 일정 HTTP 계약이다.
> ✅ **기본 비활성 일정 HTTP 계약 완료**: 동일 DB scope permit과 schedule writer를 명시 주입한 테스트 앱에만 intent/preflight/approve/reject/apply 5개 POST를 등록했다. Bearer·no-query·chat header·UUID·멱등키·중복 JSON·크기 제한·cross-chat 409를 검증했다. 운영 8091에는 schedule write capability가 없고 경로 404, scoped table 0개를 유지한다. 전체 테스트 1,395개 통과. 다음 구현 단위는 기본 비활성 Private client 일정 쓰기 어댑터다.
> ✅ **기본 비활성 일정 Private client 완료**: 명시 활성화와 activation permit이 있는 객체만 schedule submit/preflight/approve/reject/apply를 호출한다. chat scope를 모든 요청 헤더에 강제하고 임시 HTTP/SQLite에서 승인 전 무변경, add/complete/delete, replay, cross-chat 409, 인증 실패와 엄격한 응답 allowlist를 검증했다. 운영 Telegram·8091·DB에는 연결하지 않았다. 전체 테스트 1,405개 통과. 다음 구현 단위는 일정 Telegram 요청의 결정론적·비민감 identity다.
> ✅ **Telegram 일정 write identity 완료**: update/chat/user/message/action을 일정 전용 결정론적 request UUID·approval UUID·멱등키로 변환한다. identity repr에는 Telegram 숫자 ID·일정 제목·시간·메모가 없고 watchlist 도메인과도 분리된다. Telegram은 add/delete/complete에만 identity를 만들어 `schedule_bot`에 전달하며 action 불일치는 DB 실행 전에 거부한다. 아직 일정 Private client/executor에는 연결하지 않아 운영 writer 소유권은 기존 직접 경로다. 전체 테스트 1,426개 통과. 다음 구현 단위는 기본 비활성 일정 consumer executor다.
> ✅ **기본 비활성 일정 consumer executor 완료**: executor+client 이중 활성화, 동일 activation permit, 호출별 명시 승인을 요구한다. 임시 HTTP/SQLite에서 preflight→submit→approve→apply, pending/approved 재개, applied replay, reject·payload drift·cross-chat 차단, add/complete/delete를 검증했다. `schedule_bot` 주입 시 직접 DB 경로를 우회하고 API 오류 시 로컬 폴백하지 않는다. 운영 Telegram에는 executor를 주입하지 않았다. 전체 테스트 1,440개 통과. 다음 구현 단위는 알림 스케줄러의 내부 쓰기 소유권까지 포함한 일정 cutover readiness/dry-run이다.
> ✅ **일정 cutover readiness/dry-run 완료**: 사용자 mutation은 `private-data-api`, 발송 완료·사전알림·반복 진전은 `telegram-schedule-notifier`가 소유하는 비중첩 operation 경계를 고정했다. 전환·롤백은 watchlist writer를 유지한 채 일정 writer만 바꾸며 notifier를 중지하지 않는다. fresh backup·permit·승인·무폴백·소유권·순서 drift·부분 활성화를 fail-closed로 검사하고 실행 기능이나 자동 DB 복원은 제공하지 않는다. 운영 설정·서비스·DB는 불변이다. 전체 테스트 1,466개 통과. 다음 단계는 fresh clone 일정 rollback rehearsal이다.
> ✅ **fresh clone 일정 rollback rehearsal 완료**: 운영 `assistant.db`를 온라인 백업·메모리 복구 검증한 뒤 `/private/tmp/ultron-schedule-rollback-wBbn79`의 사본에서만 API 일정 추가→알림 완료 마킹→반복 진전→legacy 사용자 writer 재개→API 결과 보존→별도 emergency restore 일치를 확인했다. backup은 불변이고 운영 DB 논리 서명·일정 5건·watchlist 0건·scoped table 0개가 유지됐다. 산출물은 삭제하지 않았다. 전체 테스트 1,470개 통과. 다음 구현 단위는 일정 capability를 포함하되 기본 비활성인 atomic runtime bundle 확장이다.
> ✅ **일정 기본 비활성 atomic runtime bundle 완료**: 기존 운영 v1 bundle을 일정 비활성으로 계속 수용하고, v2의 `schedule` block이 없거나 `enabled=false`면 일정 writer/executor를 만들지 않는다. 활성 v2는 일정 API/client/executor, legacy user owner, notifier owner·operation, 동일 DB, 무폴백을 한 fingerprint로 검증한 뒤에만 양 프로세스 stack을 만든다. 부분 플래그·notifier drift·bundle drift는 fail-closed다. 실제 운영 bundle `528fcb275432…`는 재검증 결과 schedule=false이며 env/file/service를 변경하지 않았다. 전체 테스트 1,478개 통과. 다음 단계는 비설치 v2 schedule activation candidate와 runbook 확장이다.
> ✅ **비설치 v2 schedule candidate/runbook 완료**: fresh rehearsal manifest에 묶인 candidate를 `/private/tmp/ultron-schedule-candidate-922Se5/`에 600 권한으로 생성했고 combined bundle `86bc4a9a2625…`·activation fingerprint `7129d0066ec1…`를 runtime 재검증했다. installed=false이며 운영 bundle은 불변이다. writer 중첩을 막기 위해 cutover는 Telegram→Private API, rollback은 Private API→Telegram 재시작 순서로 수정·고정했다. 전체 테스트 1,484개 통과. 다음 단계는 실제 운영 일정 write cutover에 대한 명시 승인과 직전 fresh backup 재검증이다.
> ✅ **운영 일정 write cutover·실제 Telegram E2E 완료 (2026-08-26)**: 영구 backup `20260826T193342+0900`을 새로 만들고 v2 bundle `f0c651775aa4…`를 600 고정 파일에 설치했다. 이전 v1은 `/private/tmp/ultron-schedule-cutover-operational-PWTtTX/`에 보존했다. 안전 순서대로 Telegram→Private API를 재시작해 사용자 일정 mutation은 API-only, 알림 상태·반복 진전은 Telegram notifier로 분리했다. 인증 status와 격리 무데이터 replay에 이어 실제 Telegram에서 테스트 일정을 add한 뒤 `#8` delete했고 양쪽 모두 pending→approved→applied, chat version `0→1→2`를 확인했다. 최종 일정 5건·사전 알림 4건은 원상 복원됐으며 DB integrity/메모리 복구 검증과 전체 테스트 1,484개에 통과했다. 다음 Phase 2 쓰기 단위는 아직 직접 경로인 Paper `buy/sell`의 기본 비활성 계약이다.
> ✅ **Paper buy/sell 기본 비활성 계약·저장소 완료**: `private_paper_write_contract.py`가 6자리 ticker, 양수 수량·가격, 비음수 수수료, 본문 길이와 양의 정수 `slot_id` scope를 정규화한다. `private_paper_write_store.py`는 명시 DB와 `writes_enabled=True`가 있을 때만 연결하며 pending→approved→applied, 슬롯별 version, 멱등 replay, stale 만료, cross-slot 차단, 잔고·보유량 검증을 한 SQLite 트랜잭션으로 처리한다. 임시 DB에서 buy 가중평균·자본 차감, partial/full sell·자본 복원, 실패 원자성과 payload 비노출 감사를 검증했다. 운영 `paper.db`는 scoped table 0개·portfolio 1/slots 4/positions 4/trades 169로 불변이고 8091 Paper write capability도 없다. 전체 테스트 1,512개 통과. 다음 구현 단위는 writer를 명시 주입한 기본 비활성 Paper write HTTP 계약이다.
> ✅ **Paper buy/sell 기본 비활성 HTTP 계약 완료**: 별도 `paper.db` readiness permit과 writer를 명시 주입한 테스트 앱에만 intent/preflight/approve/reject/apply 5개 POST를 등록했다. Bearer·no-query·양의 정수 slot header·header/payload 일치·UUID·멱등키·중복 JSON·크기 제한·cross-slot 및 잔고 충돌을 검증했고 승인 전에는 매매 상태가 변하지 않는다. 운영 8091은 Paper write capability 없음·경로 404, 운영 DB는 integrity ok·scoped table 0개·portfolio 1/slots 4/positions 4/trades 169를 유지한다. 전체 테스트 1,527개 통과. 다음 구현 단위는 기본 비활성 Private client Paper 쓰기 어댑터다.
> ✅ **Paper buy/sell 기본 비활성 Private client 완료**: 일반 watchlist·일정 쓰기와 분리된 `paper_writes_enabled`, `paper.db` 절대 경계 permit, 명시 DB 경로가 모두 있어야만 submit/preflight/approve/reject/apply를 호출한다. slot header와 payload/응답 slot을 일치시키고 buy `total_cost`·sell `proceeds` 계산 및 응답 필드 allowlist를 재검증한다. 임시 HTTP/SQLite에서 승인 전 무변경, buy/sell, replay, cross-slot, reject, 인증·잔고 충돌을 검증했다. 운영 runtime·Paper UI·Telegram에는 연결하지 않았고 8091 404·운영 DB 1/4/4/169·scoped table 0개를 유지한다. 전체 테스트 1,538개 통과. 다음 구현 단위는 Paper UI·Telegram 직접 writer 소유권 분류와 결정론적·비민감 요청 identity 계약이다.
> ✅ **Paper direct writer 소유권·identity 계약 완료**: production `record_buy/sell` 호출을 AST로 전수 대조해 Paper UI buy/sell, Telegram intraday sell-only, 키움·콴텍 승인 batch, operator CLI의 6개 경계를 고정했다. 정상 운영 target DB writer는 `private-data-api` 하나이고 CLI direct는 rollback-only 퇴역 대상으로 분류했다. UI/Telegram 4개 caller의 actor/event/batch ordinal을 즉시 hash한 뒤 UUID·멱등키를 결정론적으로 만들며 repr에는 사용자 좌표·slot·ticker·수량·가격·본문이 없다. payload는 identity에서 제외해 retry 중 변경이 새 identity가 아닌 fingerprint conflict가 되게 했다. 신규 identity 테스트 23개가 통과했고, 동시에 추가된 weekly-report 테스트 25개를 포함한 전체 테스트는 1,586개 통과했다. 아직 runtime에는 연결하지 않았고 운영 DB 1/4/4/169·scoped table 0개를 유지한다. 다음 구현 단위는 identity+Paper client를 묶는 기본 비활성 consumer executor다.
> ✅ **Paper 기본 비활성 consumer executor 완료**: Paper 전용 permit이 일치하는 client/executor만 활성화되며 UI·키움·콴텍은 명시적 사용자 승인, Telegram intraday는 sell-only 정책 승인을 요구한다. preflight→submit→approve→apply와 pending/approved 재개, applied replay, reject, payload drift, cross-slot 사전 차단, 잔고 실패 원자성, API 장애 direct DB fallback 0을 임시 HTTP/SQLite 신규 11개 테스트로 확인했다. 운영 runtime에는 연결하지 않았고 8091 Paper write route는 404다. 운영 DB portfolio 1/slots 4/positions 4/trades 169, scoped table 0, integrity ok를 유지한다. 검증 중 외부 서비스 재시작과 함께 raw SHA-256이 `7a94a9ddd021…→adcb98c80723…`로 바뀌어 논리 불변만 확인했으며 byte-for-byte 불변은 주장하지 않는다. 전체 회귀는 1,606개 통과했다. 현재 진행률은 전체 약 70%, Phase 2 약 78%, Paper write track 60%다. 다음 구현 단위는 Paper writer ownership readiness/rollback이다.
> ✅ **Paper readiness/cutover dry-run 완료**: 현재 Paper UI와 Telegram intraday·kium·quant의 direct runtime writer 4개, operator CLI rollback-only를 고정하고 목표 DB writer는 `private-data-api` 하나로 제한했다. UI·kium·quant explicit-user와 intraday sell-only policy 승인 matrix, `paper.db` schema·fresh backup·permit·동일 DB·direct fallback 금지, consumer-first/API-writer-last cutover 및 역순 rollback을 fail-closed로 검사한다. 산출물에는 execute/apply/service 제어가 없고 운영 import·설정·서비스·DB를 바꾸지 않았다. 신규 29개와 전체 1,640개 테스트가 통과했으며 운영 8091 Paper write route는 404, DB 1/4/4/169·scoped 0·integrity ok다. 체크포인트 `2603b0e` 이후 현재 진행률은 전체 약 71%, Phase 2 약 82%, Paper write track 70%다. 다음 구현 단위는 fresh clone Paper rollback rehearsal이다.
> ✅ **Paper fresh clone rollback rehearsal 완료 (2026-08-27)**: 검증된 영구 backup `20260826T193342+0900`의 `paper.db`만 `/private/tmp/ultron-paper-rollback-G33xRZ`로 복제했다. active clone에서 API buy/sell→legacy direct buy/sell을 실행해 trades 169→173, positions 4·초기 자본 복원, rollback 뒤 API 거래 보존을 확인했다. emergency restore는 backup signature와 완전히 일치했고 운영 source·backup은 byte/logical 전후 불변이다. 산출물은 삭제하지 않았다. rehearsal direct 호출은 `verified-backup-clone-only`로 별도 인벤토리화했으며 운영 writer가 아니다. 병행 signal 관심종목 병합의 기존 테스트 2건도 개인 관심종목 읽기를 끄는 단위 경계로 교정해 signal 58개와 전체 1,661개가 모두 통과했다. 진행률은 전체 약 72%, Phase 2 약 86%, Paper write track 80%이며 다음은 미설치 runtime bundle/candidate다.
> ✅ **Paper v3 atomic runtime/candidate 완료 (2026-08-27)**: v1/v2 하위 호환을 유지하고 v3에서만 Paper block을 엄격히 검증한다. assistant/Paper DB는 같은 fresh manifest를 공유하지만 permit fingerprint는 `5515f11433be…`/`53b92536f2e8…`로 분리된다. caller policy와 legacy writer drift·부분 활성화·combined fingerprint 불일치는 fail-closed다. API·Paper UI·Telegram 세 loader가 bundle `0e5d8d10b140…`를 동일하게 확인했고 후보는 `/private/tmp/ultron-paper-candidate-jiBwT8/`에 dir/file 700/600, `installed=false`로 보존했다. 운영 고정 bundle은 v2 `f0c651775aa4…` 그대로이며 Paper route/서비스/환경은 활성화하지 않았다. 운영 DB는 1/4/4/169·scoped 0·integrity ok, 전체 테스트는 1,669개다. 진행률은 전체 약 73%, Phase 2 약 90%, Paper write track 90%이며 다음 단계는 별도 명시 승인이 필요한 운영 Paper write cutover다.
> ✅ **Paper production pre-cutover wiring 완료 (2026-08-27)**: Private API·Paper UI·Telegram이 v3 bundle일 때만 각각 Paper writer와 client/executor를 구성한다. UI 주문은 브라우저 event UUID+명시 승인, intraday는 cycle+ordinal+sell-only 정책 승인, kium/quant는 callback update+batch ordinal+명시 승인을 사용한다. v3에서는 Paper API 장애 시 직접 DB로 폴백하지 않으며 읽기도 같은 Private client를 사용한다. v2에서는 기존 운영 경로를 유지한다. 운영 고정 bundle은 여전히 v2 `f0c651775aa4…`, Paper DB 1/4/4/169·scoped 0·integrity ok이고 서비스·환경·DB 전환은 하지 않았다. 전체 테스트 1,675개 통과. 진행률은 전체 약 74%, Phase 2 약 92%, Paper write track 95%이며 다음은 명시 승인 운영 cutover다.
> ✅ **Paper 운영 v3 write cutover 완료 (2026-08-27)**: 승인 후 영구 backup `20260827T093747+0900`과 fresh clone rollback rehearsal을 검증하고 v3 bundle `1298bf8648a…`를 원자적으로 설치했다. Paper UI→Telegram consumer를 먼저, Private API writer를 마지막에 재시작했으며 중간 요청은 HTTP 500으로 fail-closed되고 direct DB fallback은 없었다. 전환 후 Paper write capability·5개 route와 같은 identity의 무데이터 preflight 200×2 동일 응답을 확인했다. 운영 Paper는 1/4/4/169, scoped resource-version/intent/audit 1/0/0, integrity ok다. PIDs는 UI/Telegram/API 23677/23706/23761이며 전체 1,675 tests가 통과했다. 진행률은 전체 약 75%, Phase 2 약 96%, Paper write track 98%다. 다음은 Paper UI 또는 Telegram kium/quant에서 사용자가 의도한 매매 1건의 pending→approved→applied→replay E2E다.
> ⚠️ **Paper 실사용 E2E·중복 의도 확인 대기 (2026-08-27)**: 콴텍 대우건설(047040) 1주 매수가 09:54:26/09:54:45에 서로 다른 source identity로 각각 pending→approved→applied됐다. 최신 요청을 같은 ID로 다시 apply하자 `replayed=true`, 거래 추가 0으로 멱등성은 정상이다. 현재 positions/trades 5/171, scoped intent/audit 2/7, 대우건설 2주·평균원가 20,630.9원, integrity ok다. 사용자가 한 건만 의도했다면 삭제·직접 DB 수정은 하지 말고 승인된 보정 방식을 먼저 정한다.
> ✅ **Paper UI 연속 제출 방지 운영 적용 (2026-08-27)**: 주문 처리 중 매수·매도 버튼을 잠그고, 성공한 주문과 내용이 같은 주문은 60초 동안 새 source identity 생성을 막는다. Paper UI 51 tests와 병행 변경을 포함한 shared worktree 전체 1,721 tests가 통과했다. Paper UI만 PID 27567로 재시작했고 운영 HTML에서 in-flight/60초 guard를 확인했다. DB는 positions/trades 5/171·intent/audit 2/7로 불변이며 보정은 사용자 의도 확인 후다.
> ✅ **Paper 잔여 direct 접근 판정 완료 (2026-08-27)**: inventory 7개를 AST로 전수 대조하고 운영 5 caller의 direct `record_buy/sell`이 executor-none legacy 분기 안에서만 가능함을 회귀 테스트로 고정했다. 현재 v3 bundle `1298bf8648a…`는 `PrivateDataClient`·`PaperTradeWriteExecutor`를 실제 구성하므로 운영 direct 분기는 도달 불가다. operator CLI는 rollback-only, rehearsal은 verified-backup-clone-only다. 남은 결정은 대우건설 2주가 사용자 의도인지 여부다.
> ✅ **Phase 2 종료·Phase 3 Shareable MCP 착수 (2026-08-27)**: 사용자가 보정 선택 대신 다음 단계 진행을 지시해 대우건설 2주는 삭제·직접 수정 없이 보존했다. 공식 Python SDK `mcp 2.1.1`을 추가하고 `shareable_mcp.py`를 stdio server로 구현했다. 도구는 get/search instrument, latest OHLCV/index/universe 5개와 정확히 일치하며 8090 API만 호출하고 공개 필드를 다시 선별한다. 실제 subprocess protocol `2026-07-28`, server `ultron-shareable`, 삼성전자 조회, Private/file/path/SQL tool 0, 전체 1,724 tests, `pip check` 정상이다. 외부 host 설정은 아직 수정하지 않았다. 다음은 MCP Inspector 검증 후 승인된 host 한 곳에 등록하는 단계다.
> ✅ **공식 MCP Inspector 검증 완료 (2026-08-27)**: Inspector CLI의 `tools/list`가 정확히 5개만 반환했고 `tools/call get_instrument ticker=005930`은 삼성전자 ticker/name/corp_code/as_of만 structuredContent로 반환하며 `isError=false`였다. Private/file/path/SQL surface는 없다. Phase 3 진행률은 약 30%, 전체 약 80%로 갱신한다. 다음은 Codex/Claude Desktop/Cursor 중 사용자가 선택한 host 한 곳에 절대 경로 stdio 설정을 등록하는 단계다.
> ✅ **Codex host 등록·실제 호출 완료 (2026-08-27)**: 공식 Codex MCP 문서와 로컬 CLI 형식을 대조한 뒤 전역 설정에 `ultron-shareable` STDIO server를 절대 Python/script 경로로 등록했다. `codex mcp get/list`는 enabled=true·env 없음·인증 없음이고 운영 8090 health는 status=ok·boundary=shareable-only다. 파일 쓰기 금지 ephemeral 새 Codex 작업이 실제 `get_instrument(005930)`을 호출해 삼성전자와 ticker/name/corp_code/as_of만 반환했다. 비대화형 기본 호출은 승인 입력 불가로 중단되어 재검증 한 번에만 approval override를 썼고 전역 자동승인은 남기지 않았다. rollback은 `codex mcp remove ultron-shareable`이며 승인 없이 실행하지 않는다. 전체 진행률은 약 85%, Phase 3은 약 50%다.
> ✅ **MCP 읽기 전용 승인·host allowlist 이중 고정 완료 (2026-08-27)**: 5개 tool 모두 공식 SDK `ToolAnnotations`의 readOnlyHint=true·openWorldHint=false를 반환한다. 힌트를 단독 신뢰하지 않고 Codex 전역 설정에도 같은 5개 enabled_tools와 default_tools_approval_mode=writes를 고정했다. 실제 stdio smoke에서 protocol 2026-07-28·5개 annotation·삼성전자 공개 4필드가 일치하고, 별도 approval override 없는 ephemeral·read-only 새 Codex 작업의 실제 호출도 성공했다. MCP/Data API 30 tests와 shared worktree 전체 1,725 tests가 통과했다. 진행률은 전체 약 88%, Phase 3 약 60%다.
> ✅ **Phase 3 로컬 STDIO MVP 완료 (2026-08-27)**: 공개 API의 유일한 미노출 endpoint인 시장 전체 factor snapshot(KOSPI fundamentals 890/market-cap 917종목)은 과대 응답이라 직접 노출하지 않고, 단일 ticker만 공개 6개 재무지표와 시가총액으로 축소하는 `get_latest_fundamentals`를 추가했다. 서버·Codex allowlist는 6개로 일치하며 새 Codex 작업의 삼성전자 실제 호출이 성공했다. `audit_shareable_mcp.py`는 command/script same-file, enabled tools, writes 승인, env 미전달, stdio-only, 6 annotation과 instrument/fundamentals 호출을 검사해 ready=true다. 경로 NFC/NFD false negative는 문자열 완전 일치 대신 실제 file identity 비교로 교정했다. 전체 1,732 tests 통과. North Star 로컬 아키텍처와 Phase 3 로컬 MVP는 100%이며 원격 HTTP는 별도 선택 확장이다.
> ✅ **병렬 착수 전 direct DB 드리프트 정리 (2026-08-27)**: `signal_bot` 관심종목과 `slot_diversify` Paper 포지션 진단을 8091 Private API-only로 전환하고 실패 시 DB 폴백을 제거했다. `seed_watchlist_from_bots.py`는 직접 DB 적용 기능을 없애고 미리보기 전용으로 보존했다. 회고·슬롯 위험 문서의 구현 상태도 코드와 정합화했으며 전체 1,736 tests와 `git diff --check`가 통과했다. 작업 트리는 modified 20/untracked 23, 총 43개로 아직 커밋 전이다.
> 투자 전략 트랙을 재개할 때는 `docs/다음작업_손절_상관분석.md`를 먼저 읽는다.
> (system_info·paper 성과·피드백 루프·trade_analytics는 아래 완료 항목 참조.)

# IPO Bot — 다음 세션 인수인계 (v3.35)

## 현재 버전
**ipo_bot.py v3.34 · paper_ui.py v3.35 · router.py v3.39 · signal_bot.py v3 · telegram_bot v3.41 · news_bot v1 · agent_bot v1 · action_scheduler v1 · research_bot v1** — 2026-06-02  
**상태: paper_ui IPO 탭 연동 완료 (정수키 dict·점수 필드·showToast 버그 수정) + 테스트 paper_ui 40 / ipo_bot 95 PASS ✅**

## 🚀 다음 세션 시작 가이드

**먼저 읽을 파일** (이전 세션 패턴과 동일):
1. `~/울트론/ai-agent/docs/NEXT_SESSION.md` — 이 파일 (IPO Bot 영역의 working state)
2. `~/울트론/local-ai-agent-plan.md` — 전체 로드맵 (필요 시 참고. 굳이 처음부터 다 안 읽어도 됨)

**현재 검증된 상태**:
- `ipo_bot.py` **v3.34** — 전체 파이프라인 + DART 밴드 백필 + 95개 테스트 PASS
- `paper_db.py` — IPO 함수 정수키 dict 버그 수정 완료, 37개 테스트 PASS
- `paper_ui.py` **v3.35** — IPO 탭 JS 버그 3종 수정, 40개 테스트 PASS
- **합계 172개 단위 테스트 통과** (네트워크 없이 mock 기반)

**샌드박스 환경 셋업** (Cowork bash 안에서):
```
pip install pytest fastapi uvicorn pandas httpx pykrx --break-system-packages -q
```
- 실제 `data/paper.db`: portfolios 1 / slots 3 / positions 7 / trades 20 (이전 세션 누적)
- test_paper_ui.py는 `_conn` 리다이렉트로 격리됨 → 실제 DB 오염 안 됨
- ipo_bot/paper_ui 모두 외부 호출 mock으로 hermetic

**파일 편집 주의**:
- `Edit` 툴이 `$HOME/울트론/` 경로에서 차단됨 (Unicode 정규화 이슈)
- 대안: `mcp__workspace__bash`로 마운트 경로(`/sessions/.../mnt/*/ai-agent/...`)에 Python 스크립트 작성해 정밀 치환 (이 세션의 `.bak*` 백업 파일들이 동일 패턴 사용 흔적)

**다음 단계 후보 — 난이도·환경 의존도**:

| # | 후보 | 코드 분량 | 환경 의존 | 비고 |
|---|---|---|---|---|
| 1 | Telegram /analyze 실환경 테스트 | 소 | **텔레그램 봇 구동 필요** | lockup 인자 포함 흐름 점검 |
| 2 | score_demand 구간 세분화 | 중 | 실데이터 권장 | 현재 1000~1500 단일 구간 17점 → 1100/1300 추가 |
| 3 | DART 밴드 정규식 정밀화 | 중 | 캐시 활용 가능 | `data/ipo_samples/`·DART cache로 hermetic 가능 |
| 4 | paper_ui IPO 탭 브라우저 점검 | 소 | **`paper_ui --host` 실행** | 스캔→청약→상장 흐름 시각 확인 |

**추천**:
- 코드만으로 자기완결적인 작업을 원하면 **후보 3 (DART 정규식 정밀화)** — 캐시된 증권신고서 텍스트(`data/ipo_samples/*.txt`)를 회귀 테스트로 묶어 패턴을 보강할 수 있음
- 실환경 확인부터 하려면 **후보 4 (paper_ui 브라우저 점검)** — `agent_services.sh status`로 paper(8080) 떠 있는지 확인 후 `http://localhost:8080/` → IPO 탭

**세션 첫 행동 권장**:
1. NEXT_SESSION.md(이 파일) 끝까지 읽기
2. AskUserQuestion으로 후보 1~4 중 어느 것 진행할지 확인
3. TaskCreate로 작업 분해 → 진행

## 완료된 작업

### ✅ 원칙 Wiki/RAG 정합성 정리 (2026-08-22) ← NEW
- DB GAPS 대회 자료 2건 제거, LanceDB 잔존 청크도 동기화 삭제.
- 깨진 Wiki 링크 교정 + `IPO_매력지수_기준.md`를 현재 코드 기준으로 추가.
- 핵심 자산배분을 위험 65% / 안전 30% / 현금 5%로 정합화.
- 증분 인덱서가 삭제된 파일 청크와 짧은 Wiki 노트를 자동 동기화하도록 보강.
- 정리 당시 Wiki 22개 = 인덱스 22개, 125청크였고, 현재는 24개/134청크로 증가했으며 stale/missing/outdated 0건을 유지한다.
- 전체 회귀 테스트 1,030개 통과.

### ✅ Phase 1 Private/Shareable 물리 분리 완료 (2026-08-22) ← NEW
- 활성 레이아웃을 `legacy`에서 `private-v1`으로 전환했다.
- `data/private/paper.db`는 legacy와 행 수가 일치한다: portfolios 1 / positions 2 / trades 155 / slots 4 / ipo_records 0.
- `data/private/assistant.db`에 events 5 / event_pre_notifications 4 / watchlist 0을 병합했다.
- RAG는 현재 Vault 기준 24문서 / 134청크이며 missing/stale/outdated 0건이다.
- `data/shareable`에서 chat_id·포트폴리오·거래·개인 상태 파일이 발견되지 않았다.
- Private 파일 600 / 디렉터리 700, 전환 후 `paper.db`·`assistant.db` 온라인 백업과 메모리 복구를 검증했다.
- Paper UI 8080, Telegram, watch_raw를 새 경로로 재기동했다. Tapnow 8082와 Ollama 11434는 중단하지 않았다.
- legacy DB·RAG·로그는 롤백용으로 보존하며 삭제는 별도 승인 사항이다.
- 전환 직후 전체 회귀 테스트 1,048개 통과. 다음 트랙은 Phase 2 API 서버화다.

### 🚧 Phase 2 Shareable 데이터 API 운영 전환 (2026-08-22) ← NEW
- `data_api.py`: `127.0.0.1:8090` 전용 FastAPI 골격. 비루프백 주소는 실행 단계에서 거부한다.
- `shareable_store.py`: `data/shareable` 고정 참조, ticker 검증, symlink·루트 이탈 차단, 응답 필드 allowlist.
- API: health / 종목명 검색 / 종목 메타 / 최신 universe / 최신 OHLCV 캐시. Private·범용 파일·SQL 라우트 없음.
- `data_api_client.py`: loopback URL만 허용하고 응답을 재검증하는 읽기 클라이언트.
- `invest_bot`: `AI_AGENT_DATA_API_ENABLED=1`일 때 종목 해석을 API로 우선 조회하며, 미기동·오류·미스는 기존 캐시/pykrx로 폴백한다.
- 실제 Shareable 캐시로 삼성전자 검색·메타, SK하이닉스 최신 OHLCV 조회 성공. Private 경로는 HTTP 404.
- `quant_bot`은 요청 end_date와 API `as_of`가 같을 때만 OHLCV를 채택하고, 불일치·장애 시 기존 디스크 캐시/pykrx로 폴백한다. 첫 연결 장애 뒤 30초 cooldown으로 대량 스캔의 반복 timeout을 막는다.
- 실제 8090 HTTP에서 `invest_bot` 이름/티커 해석과 `quant_bot` SK하이닉스 OHLCV 280행 조회를 확인했다. 서버 종료 후 같은 OHLCV 280행이 디스크 캐시에서 복구되는 것도 확인했다.
- `smoke_data_api.py`로 기동·readiness·invest/quant 소비·Private 404·소유한 자식 종료·종료 후 폴백을 자동 검증했다. 실제 결과 API 280행 / 폴백 280행 / 서버 종료 true.
- `agent_services.sh`에 Data API launchd 등록·readiness 대기·로그·상태 관리를 추가했다. API가 준비된 다음 Telegram/Paper가 시작된다.
- 실제 운영 plist의 Telegram/Paper에 `AI_AGENT_DATA_API_ENABLED=1`을 적용하고 재시작했다. Data API 8090 health 200, Paper 8080 HTTP 200, Tapnow 8082·Ollama 11434 유지.
- 디스크 읽기를 막은 소비 테스트에서 삼성전자 식별과 SK하이닉스 OHLCV 280행을 API로 확인했고, 사용할 수 없는 API를 주입한 테스트에서도 기존 캐시 280행 폴백을 확인했다.
- 1차 launchd 운영 전환 시점 전체 회귀 테스트 1,085개 통과.
- `GET /v1/shareable/universes/{market}/latest` 계약을 추가했다. market은 KOSPI200/KOSDAQ150/합집합 allowlist만 허용하고 유효한 6자리 ticker/name만 반환한다.
- `kium_bot`은 당일 universe와 기준일 일치 OHLCV를 API로 우선 읽는다. 오래된 캐시·장애·미스는 기존 디스크/pykrx로 폴백하며 force refresh는 기존 수집 의미를 유지한다.
- 실운영 API에서 universe 기준일 `20260803`을 확인해 오늘 데이터로 오인하지 않으며, pykrx 폴백을 막은 검사에서 SK하이닉스 OHLCV 280행을 API로 읽었다.
- `market_data_collector.py`를 시장지수 캐시 쓰기 소유자로 추가하고 `GET /v1/shareable/market-indices/{index}/latest` 계약을 구현했다. 날짜·종가 외 필드는 노출하지 않는다.
- KOSPI는 FinanceDataReader `KS11`로 363행을 수집했고 최신 실제 거래일은 `20260821`이다. 매일 16:20 launchd가 갱신하며 `kium_bot`은 최근 API 캐시를 우선 사용한다.
- 기존 VKOSPI 코드 `1003`은 실제로 KOSPI 중형주였으므로 제거했다. 검증된 무인증 VKOSPI 소스가 없어 자동 수집하지 않으며 운영 API는 캐시가 없을 때 404, 소비자는 `None`으로 축소 동작한다.
- universe/OHLCV 외부 수집과 Shareable 원자적 쓰기를 `market_data_collector.py`로 이동했다. `kium_bot`·`quant_bot`은 API/캐시 소비와 계산만 담당하며 파일 쓰기를 구현하지 않는다.
- 실제 SK하이닉스 OHLCV 14거래일을 운영 캐시와 분리된 임시 경로에 수집했다. KRX 최신 universe 갱신 실패를 모의했을 때도 운영 API의 마지막 스냅샷 199종목을 최후 폴백으로 유지했다.
- 재기동 후 Data API 8090 health, Paper 8080 HTTP 200, Telegram 시작, Tapnow 8082·Ollama 11434 공존을 확인했다.
- ticker-map 외부 조회·검증·원자적 쓰기와 fundamental/시가총액 외부 조회·필드 정제를 `market_data_collector.py`로 이동했다. `invest_bot`·`quant_bot`에는 호환 어댑터와 계산만 남겼다.
- pykrx가 계정 식별자를 표준 출력에 기록하던 동작을 수집 경계에서 차단했고, 빈 ticker-map은 성공으로 캐시하지 않는다.
- KRX 비밀번호 갱신 후 실수집 복구를 확인했다: ticker-map 2,687, KOSPI fundamental 890/시가총액 917, KOSDAQ fundamental 1,750/시가총액 1,770종목. 비거래일에는 factor 기준일을 최신 거래일 `20260821`로 후퇴한다.
- `GET /v1/shareable/factors/{market}/latest`를 추가하고 `quant_bot`을 API 우선으로 전환했다. collector를 막은 운영 검사에서 KOSPI 890/917종목을 API만으로 읽었다.
- `invest_bot` OHLCV 직접 pykrx 조회를 제거했다. 실제 삼성전자 14거래일 full OHLCV(date/open/high/low/close/volume)를 격리 수집했고, 운영 API의 기존 280행만으로 지표 계산도 성공했다.
- 매일 16:20 collector 일정은 시장지수뿐 아니라 ticker-map·KOSPI/KOSDAQ factor·universe까지 갱신한다. dataset별 실패는 격리한다.
- KRX universe 구성종목 엔드포인트는 인증 복구 후에도 빈 응답이라 새 캐시를 쓰지 않으며 마지막 KOSPI200 199종목 API 스냅샷을 유지한다.
- Paper 8080, Tapnow backend 8082, Data API 8090, Ollama 11434 공존과 Telegram 재기동을 확인했다.
- `private_data_api.py` 인증 골격을 추가했다. 기본 8091이며 8080/8082/8090/11434를 거부하고, Shareable API·Telegram과 토큰을 공유하지 않는다.
- 표면은 무인증 `/health`, 인증 필수 status/watchlist, chat 범위 필수 일정
  `/v1/private/schedule/events[/upcoming]`이다. capabilities는 `watchlist:read`와
  `schedule:read`, `writes_enabled=false`이며 Paper·파일·SQL 라우트는 없다.
- OpenAPI/Swagger/ReDoc를 끄고 모든 응답에 `Cache-Control: no-store`를 적용했다. 실제 Private DB를 열거나 수정하지 않는 테스트 골격이다.
- 64자 운영 전용 token을 `.env`(권한 600)에 생성하고
  `com.hyunjun.ai-agent.private-data-api` launchd를 등록했다. plist·로그에는 token을 넣지 않는다.
- 실제 8091 HTTP에서 health 200, 무인증·쿼리 token 401, 올바른 Bearer 200,
  모든 응답 `no-store`를 확인했다. Paper 8080·Tapnow 8082·Shareable 8090·Ollama 11434는 유지된다.
- query-string token이 기본 접근 로그에 남는 문제를 스모크에서 발견해 token을 즉시
  교체·무효화하고 Uvicorn access log를 비활성화했다. 현재 token은 plist·로그에 없다.
- `private_read_store.py`는 고정 `assistant.db`를 SQLite `mode=ro`+`query_only`로 읽고
  ticker·name·created_at만 최대 500건 반환한다. 경로·테이블·SQL은 HTTP 입력이 아니다.
- `private_data_api_client.py`는 loopback 8091·Bearer header·no-store·응답 allowlist를
  재검증하고 redirect·과대 응답을 차단한다.
- Telegram plist에 `AI_AGENT_PRIVATE_API_ENABLED=1`을 적용했다. list는 API 우선/직접 DB
  폴백이며 add/remove는 기존 직접 쓰기를 유지한다. plist에는 token이 없다.
- 실제 운영 watchlist 0건 조회 전후 DB 행 수와 파일 해시가 동일했고, 로컬 DB 폴백을
  막은 Telegram 실행 검사에서도 API-only 응답에 성공했다.
- 일정 저장소는 고정 assistant DB를 `mode=ro`+`query_only`로 읽고 숫자형
  `X-AI-Agent-Chat-ID` 헤더로 범위를 강제한다. 응답에는 chat_id·알림 상태·생성 시각을
  포함하지 않고 최대 100건만 반환한다.
- 실제 전체 일정 5건/다가오는 일정 0건, 무인증 401/chat 누락 400, DB 행 수·파일 해시
  무변경, Telegram API-only 실행을 확인했다. 일정 add/delete/complete와 알림 스케줄러는
  직접 경로를 유지한다.
- Paper portfolio/position은 고정 `paper.db` read-only 라우트로 추가했다. Paper UI는
  `AI_AGENT_PRIVATE_API_ENABLED=1`에서 API 우선, 장애 시 DB 폴백이다. slot 필터는 API
  쿼리가 아니라 클라이언트 로컬 필터이며 이 시점에는 slots/trades·매수/매도/IPO가 직접 경로였다.
- 운영 portfolio 1 / position 9, 무인증 401, 안정화 뒤 DB 해시·논리 스냅샷 무변경을
  확인했다. Paper 8080·Shareable 8090·Private 8091·Ollama 11434는 정상이며 Tapnow 8082는
  현재 리슨하지 않는다.
- slots/trades를 고정 read-only 라우트로 추가했다. 슬롯은 dashboard 집계까지 서버에서
  계산하고, 거래 slot/limit은 Private API query 없이 클라이언트에서 필터링한다.
- 운영 slots 4 / trades 164, Paper UI slots 4 / 최근 trades 100, 무인증 401, 조회 전후
  DB 파일 해시·논리 스냅샷 무변경을 확인했다. 전체 회귀 테스트 1,182개 통과.
- IPO records/stats를 최신 500건·등급 통계 20건 allowlist로 추가했다. factors는 JSON
  객체만 허용하고 subscribe/close 쓰기는 직접 경로다. 운영 0/0건 빈 목록, 무인증 401,
  Paper UI 동일 응답과 조회 전후 DB 불변을 확인했다. 전체 회귀 테스트 1,192개 통과.
- `paper_metrics.py` 순수 FIFO 계산 계층을 추가하고 performance/myquant-tags를 API 우선으로
  전환했다. 운영 직접 계산과 결과가 동일하며 performance 4행·태그 0건, 원시 거래 비노출,
  무인증 401, 조회 전후 DB 불변을 확인했다. 전체 회귀 테스트 1,199개 통과.
- Paper UI 일반 런타임 읽기는 전환 완료다. 남은 직접 접근은 쓰기, Telegram 승인 전제
  조회/쓰기, 일정 알림 스케줄러, 내부·오프라인 분석 예외다.
- 운영 watchlist write cutover 완료: Private API가 단일 writer이고 Telegram add/remove는
  동일 atomic bundle의 API executor만 사용한다. 실제 삼성전자 add/remove로 version 2와
  양쪽 감사를 확인했다. 일정 chat 격리 계약·저장소도 완료했으며 다음 구현 단위는 기본 비활성
  일정 client 쓰기 어댑터다. 테스트 전용 HTTP 계약은 완료됐다.

### ✅ 개인 관심종목 자연어 관리 (watchlist v1) ← NEW
- `watchlist_store.py`: 당시 ignored `data/private.db`에 구현했고 현재는 `data/private/assistant.db`로 병합·전환. ticker PK 멱등 추가·삭제·조회, MCP 미노출.
- `watchlist_bot.py`: 기존 KRX 종목 해석기를 재사용해 유효 종목만 저장.
- `router.py`: `watchlist_bot(add/remove/list)` Function Calling 등록 + 명백한 발화 결정론 안전망.
- `telegram_bot.py`: 라우팅 결과를 실행하고 추가·중복·목록·삭제 응답.
- 핵심 자산배분 투자 종목 95% + 현금 5%(`핵심_자산배분_포트폴리오.md`)와 개인 관심종목을 분리.
- 구현 시점 전체 회귀 테스트 1,025개 통과.

### ✅ 매매 정밀 진단 추가 (trade_analytics v1.1) ← NEW
- **요청**: "손절선이 너무 타이트한지, 손실이 특정 종목/시기에 몰렸는지" 파고드는 분석.
- **실측 반전**: 손절선은 타이트한 게 아니라 **오히려 늦게 실행**됨 — 손절 30건 평균 -14.2%
  (손절선 -7%인데 30건 전부 손절선보다 깊게, 19건은 5%p+ 초과). 30분 모니터라 갭/급락에
  -7%를 훌쩍 넘겨 체결. + 휩쏘 4건(손절 후 3~10일 재매수) + 손실집중 워스트3=총손실 42%.
- **추가 함수(순수)**: stop_loss_diagnosis(슬리피지)·whipsaw(손절후 재매수)·loss_concentration.
  format_precision로 리포트에 "🔎 정밀 진단" 섹션 추가, llm_summary 프롬프트에도 반영.
  format_deep/deep_report에 trades 전달. 라우팅 변경 없음(analysis 토픽 재사용). +8 테스트, 회귀 759 PASS.
- **다음 후보**: 손절 실행 지연 수정(30분→더 촘촘/장중 실시간, 갭 시 시장가), -7% 재검토,
  워스트 종목/국면 상관(6~7월 손실이 특정 섹터·거시국면에 몰렸는지).

### ✅ 매매 심화 분석 (trade_analytics v1 — 트레이드 단위 4차원) ← NEW
- **요청**: "지금까지의 데이터를 분석하는 기능". 슬롯 성과 리포트(paper_analytics)를 보완하는
  트레이드 단위 분석. 사용자 선택: 4차원 모두.
- **scripts/trade_analytics.py 신규**: FIFO 라운드트립(compute_roundtrips) 위에—
  (1) 매매 습관: 손익비(PF)·손익크기비(payoff)·평균이익/손실·손절vs익절 빈도·평균 보유일,
  (2) 종목별 실현손익 베스트/워스트(by_ticker),
  (3) 월별 실현손익 + perf_history 스냅샷 추세(monthly_pnl),
  (4) LLM 자연어 총평(llm_summary, 로컬 Gemma, 주입 가능·graceful).
  순수함수 + deep_report()만 paper_db/LLM 호출. 15 테스트.
- **라우팅**: "매매 습관/손익비/거래 분석/성과 분석/심화 분석" → system_info topic "analysis"
  → trade_analytics.deep_report(LLM 총평 포함). 개별종목 분석(invest)·빠른성과(performance)·
  비중제안(feedback)과 충돌 안 함. system_info 디스패치 재사용이라 telegram 추가 배선 불필요.
- **테스트**: trade_analytics 15 + 라우팅. 회귀 751 PASS. 프롬프트 12927자(<13000).
- **실측 인사이트(2026 5~7월, 완결 42건)**: 승률 26%·PF 0.68(순손실)·손익크기비 1.92
  (크게 벌고 자주 작게 잃는 구조가 무너짐) · 손절 30/익절 10 · 워스트 대우건설 -213만 ·
  월별 5월 +598만 → 6월 -555만 → 7월 -656만(연속 악화). ← 슬롯 리포트엔 안 보이던 진단.
- **사용자 액션**: 봇 재시작 후 "매매 습관 분석해줘"/"성과 분석해줘"로 LLM 총평 포함 리포트 확인.


### ✅ 승인형 적용 골격 (strategy_feedback v0.1, 자동실행/텔레그램 미연결) ← NEW
- **목적**: 비중 제안 → (사용자 승인 시) 실제 슬롯 allocation_pct 반영의 골격만 선구축.
- **strategy_feedback**: current_suggestions(db)로 proposal 데이터 로직 추출·재사용.
  build_apply_plan(suggestions, min_delta=0.5%p)로 노이즈 컷 후 적용 계획 생성.
  apply_plan(plan, setter, dry_run=True)는 **기본 dry_run**(미리보기, setter 미호출) —
  dry_run=False(명시 승인)일 때만 setter(slot, fraction) 호출. format_apply_plan 미리보기 포맷.
  setter는 **주입식**(실제 paper_db 쓰기는 아직 연결 안 함) → 순수·테스트 가능.
- **가드**: |delta|<0.5%p 슬롯 제외 / 중립이면 빈 계획(no-op) / dry_run 기본.
- **테스트**: strategy_feedback +7(계획·dry_run·승인 실행·no-op·포맷). 회귀 729 PASS.
- **남은 연결(미구현, 데이터 대기)**: (1) paper_db에 set_allocation(slot,pct) writer 추가,
  (2) 월간 리밸런싱 메시지에 "비중 적용" 승인 버튼(callback) → apply_plan(dry_run=False, setter=paper_db writer),
  (3) 적용 이력 로깅. 자격 슬롯(완결10건↑) 2개↑ + history 누적 후 활성화 권장.


### ✅ 성과 스냅샷 누적 + EWMA 평활 (v3.46, 피드백 루프 2단계) ← NEW
- **목적**: 비중 제안을 단발 스냅샷이 아니라 시계열 평활값으로 → 소표본 단발 변동 방어 + 추세 입력 확보.
- **paper_analytics**: record_snapshot(stats,now,path)/load_history(path) — 타임스탬프별 슬롯 지표
  (n_closed·수익률·샤프·pnl·승률)를 data/cache/perf_history.json에 append(JSON, tmp 격리 테스트).
- **strategy_feedback**: ewma_score(series,halflife)·scores_from_history(history) 추가. 반감기 4(스냅샷),
  최신 가중↑. compute_tilts(scores=) 주입 인자 추가 → 평활점수로 자격슬롯 점수 대체.
  proposal()은 history가 MIN_SNAPSHOTS(3)↑면 EWMA 평활 사용, 아니면 최신 스냅샷(기존 동작).
- **telegram**: 주간 성과 액션(_run_action "performance")이 리포트 생성 시 성과 스냅샷도 적재
  → "매주 월 9시 성과 리포트" 예약이 돌 때마다 history가 쌓임.
- **테스트**: paper_analytics +2(스냅샷 저장/로드) · strategy_feedback +4(EWMA·평활·주입). 회귀 722 PASS.
- **다음 단계(미구현)**: history가 충분히 쌓이고 자격 슬롯(완결10건↑) 2개↑가 되면 제안이 비중립화됨.
  그 후 (1) 월간 리밸런싱에 ✅승인 시 슬롯 allocation_pct 실제 조정(승인형 적용), (2) 추세 차트.


### ✅ paper 성과 고도화 + 콴텍 정상화 (v3.46) ← NEW
- **콴텍봇 정지 원인 규명·수정**: quant_monthly_rebalance가 "첫 영업일 09:30 정시"에 24h 잡
  tick이 정확히 맞아야만 발화 → 봇 재시작 시 tick 위상 어긋나 영영 안 떴음(콴텍 슬롯 5/18 이후
  정지·flag 파일 없음). `action_scheduler.monthly_rebalance_due()` 순수함수 추가(catch-up:
  이번 달 미발송 + 첫 영업일 지났으면 봇 켜진 즉시 발화) + `_first_business_day_passed()` 헬퍼.
  telegram 잡 게이트 교체. 테스트 5(catch-up 케이스).
- **scripts/paper_analytics.py 신규**: paper_db.performance_stats(FIFO 실현손익·승률·MDD·샤프)
  위에 얹는 분석 계층. summarize()(포트폴리오 집계·best/worst·정체슬롯 진단)·format_report()
  순수함수 + report()(DB 연동). 18 테스트.
- **성과 라우팅**: "성과/수익률/승률 어때"(self/paper 문맥) → system_info topic "performance"
  결정론 단락 → paper_analytics.report(). "매주 월 9시 성과 리포트 보내" → action_schedule
  performance 액션(예약 우선). ACTION_LABELS·_AS_ACTIONS·_run_action에 performance 추가.
- **테스트**: paper_analytics 18 + system_info 누적 + action_scheduler 23. hermetic 회귀 696 PASS.
  프롬프트 12905자(<13000).
- **실측 성과(2026-06 기준)**: 콴텍 +197만(완결1·승률100%) / 키움 +4만(완결22·승률36%·MDD20%·샤프0.9·보유3)
  / IPO 0건(정체).
- **사용자 액션**: 봇 재시작. "내 성과 어때"·"매주 월 9시 성과 리포트 보내" 검증.
  콴텍은 다음 첫 영업일(또는 봇 재가동 시 catch-up)에 리밸런싱 추천이 떠야 정상.

### ⏭️ IPO 슬롯 진단 결과 + 남은 결정(미구현, 사용자 판단 필요)
- **0건인 이유**: (1) 주간 스캔(월 09:10)은 돌고 있으나(flag W20~W24) **A등급↑만 알림**
  (IPO_MIN_GRADE={{A++,A+,A}}) → 그 주들에 A등급 종목이 없었을 가능성. (2) 알림 떠도 ✅승인해야
  ipo_upsert 기록. (3) **상장일 자동 정산 잡이 없음** — ipo_close(공모가 대비 수익률)는 paper_ui
  웹(/api/ipo/close)에서 수동 호출만 존재. 따라서 구독해도 실현수익률이 자동으로 안 잡힘.
- **남은 결정**: (a) A등급 필터를 낮출지(B 포함?) — 알림 빈도 vs 품질 트레이드오프(상품 결정).
  (b) 상장일 자동 정산 잡 추가 여부 — 상장가 데이터소스(pykrx/네이버) 필요·실환경 검증 필수.
  결정되면 구현 예정.

### 🐛 콴텍 catch-up 첫 발동 시 크래시 수정 + 비중 제안 연결 (v3.46 fix) ← NEW
- **증상**: catch-up은 정상 발동(잡이 떠 추천까지 계산)했으나 diff 계산 줄에서
  `getattr(r, "ticker", r.get("ticker",""))` — 파이썬이 기본값 `r.get(...)`을 항상 먼저
  평가 → 추천이 StockRecommendation(객체)라 `.get` 없음 → AttributeError로 잡 크래시
  (플래그 미기록이라 매 tick 재시도하며 계속 실패). **기존 잠복 버그**(전엔 잡이 안 떠 안 드러남).
- **수정**: `{(r.ticker if hasattr(r,"ticker") else r.get("ticker","")) for r in recs}`로 객체/ dict
  모두 안전 처리. 승인 콜백은 이미 `_rec_attr` 헬퍼라 무관. py_compile + 양쪽 케이스 검증.
- **비중 제안 온디맨드 + 첨부**: system_info topic "feedback"(“비중 제안/조정/리밸런싱 제안”)
  → strategy_feedback.proposal(). 월간 리밸런싱 메시지에 제안 블록 첨부(읽기전용, 표본 얇으면 중립).
- **사용자 액션**: 봇 재시작 → 60초 후 콴텍 추천이 Traceback 없이 떠야 정상. flag 파일 생성 확인.

### ✅ 성과→전략 피드백 루프 골격 (strategy_feedback v0, 추천 전용) ← NEW
- **결정 반영**: IPO는 현행 유지(A등급↑ 필터·상장일 수동 정산 → 코드 변경 없음).
  피드백 루프는 "지금 골격 구축" 선택 → 자동 적용 없이 추천만 내는 v0 작성.
- **scripts/strategy_feedback.py 신규**: performance_stats + 현재 슬롯 비중 → 비중 조정 제안.
  순수·결정론. 가드: MIN_SAMPLE(10건) 미만 슬롯 고정 / 자격 슬롯 2개 미만이면 전부 중립 /
  델타 합=0(총비중 보존) / 슬롯당 ±MAX_TILT(5%p) 캡 / 점수=수익률+샤프. proposal()이 실데이터로
  추천 문자열 생성. **telegram 리밸런싱 잡에 연결 안 함**(데이터 누적·검증 후 다음 단계). 13 테스트.
- **현재 동작**: 표본이 얇아(콴텍 1건<10, 키움만 자격) 전부 중립 — 의도된 보수적 가드.
- **다음 단계**: 콴텍 정상화·거래 누적으로 완결 10건↑ 슬롯이 2개 이상 되면 실제 제안 발생.
  검증 후 (1) 월간 리밸런싱 추천에 비중 제안 첨부, (2) 6개월 EWMA로 점수 평활, (3) 승인형 적용.

### ⏭️ (이전) 성과→전략 피드백 루프 (설계만)
- **목표**: paper 성과를 봇 스코어링/비중에 되먹임(예: 부진 슬롯·전략 비중 축소).
- **선행 조건**: 위 성과 분석 + 데이터 누적(콴텍 정상화·IPO 실현). 표본이 충분(완결 N건↑)해야 의미.
- **설계 후보**: performance_stats → 슬롯별 (수익률·샤프) → 다음 리밸런싱 시 슬롯 자본 비중을
  완만히 조정(상한/하한 가드). 과적합·소표본 위험 → 보수적 가중(예: 6개월 EWMA, 최대 ±5%p).
  결정론·순수함수로 분리해 테스트 후 telegram 리밸런싱 잡에 연결.


### ✅ system_info v1 — 봇 자기/시스템 인식 (router v3.45 · research v1.1 · telegram v3.45) ← NEW
- **문제**: "paper 주소 뭐야"·"리밸런싱 됐어?"·"무슨 봇 돌고 있어?" 같은 메타 질문이
  wiki RAG/웹 검색으로 새서 "못 찾음" 실패. 라우팅에 "내 시스템/내 상태" 갈래가 없었음.
- **scripts/system_info.py 신규**: 결정론 자기인식 응답기. topic별(access/service/bots/
  automation/signal/rebalance/scan/status) 실제 상태를 읽어 문자열 반환. 플래그 파일
  (signal_last/quant_rebalance_last/kium_weekly_last/ipo_weekly_last.json)·action_scheduler
  last_fired에서 "마지막 실행 시각" 산출. 모든 함수 cache_dir/sched_path 주입 → tmp 격리 테스트.
- **router v3.45**: `_detect_system_info(query)` 결정론 단락(LLM 미호출) — action_schedule
  뒤·news 앞에 배치해 "자동작업 목록"은 그대로 action_schedule로. KNOWN_TOOLS + 1줄 spec(10c)
  + validator 추가. 프롬프트 12891자(<13000).
- **research_bot v1.1**: `is_self_referential(query)` 추가(내/우리/시스템/봇/리밸런싱/포지션/
  슬롯/주소/포트/자동작업 단서). telegram knowledge 분기 가드: `rag_is_weak and not
  is_self_referential` 일 때만 웹 폴백 → 자기참조 질문은 위키 답변에 머묾.
- **telegram v3.45**: `elif tool == "system_info"` 분기 + import. knowledge 웹폴백 가드.
- **테스트**: tests/test_system_info.py 41개(모듈·라우터 감지·단락·research 가드) PASS.
  hermetic 회귀 672 PASS(이전 631 + 41, 무회귀). lancedb/pykrx 필요 모듈은 샌드박스 제외(변경 없음).
- **사용자 액션**: 봇 재시작 후 텔레그램에서 "paper 주소 뭐야"(→8080)·"리밸런싱 됐어?"·
  "무슨 봇 돌고 있어?"·"마지막 신호 언제?" 검증. (실환경에서 quant_rebalance_last.json은
  아직 없을 수 있음 → "기록 없음"이 정상)


### ✅ research_bot v1 — RAG 미스 시 웹검색→정리→wiki 저장 (telegram v3.44) ← NEW
- **요청 흐름 완성**: 명령→봇 분기(라우터)→내 위키 RAG→없으면 웹 검색→정리해 wiki 저장→답변.
- **scripts/research_bot.py 신규**: `rag_is_weak(chunks, 0.50)`(거리 임계로 위키 커버 판정) +
  DDG 무료 검색(`_ddg_search_raw`)+`parse_ddg`(bs4/정규식) + `_fetch_page` + Gemma `_summarize` +
  `research()` 오케스트레이션(검색→본문→요약→inbox 저장→답변). 외부호출 전부 주입/분리, 14 테스트.
- **telegram v3.44**: knowledge_bot 분기에서 `rag_is_weak`면 research로 폴백 → save_to_inbox(어댑터로 인자순서 보정)
  → watch_raw가 wiki로 정제. 위키에 없던 질문도 다음엔 RAG로 답 가능(자기 성장).
- **검증**: 전체 회귀 299 PASS.
- **사용자 액션**: 봇 재시작. "스토캐스틱 RSI Band Walk 뭐야" 등 위키에 없는 질문 → 웹 정리+저장 동작 확인.
  DDG HTML 스크래핑은 변동 가능 — 0건이면 parse_ddg 셀렉터 보정 필요(실환경 검증).
- **한계/주의**: 무료 DDG라 가끔 차단/빈 결과 가능. 품질은 Gemma 요약 수준. 저장 노트는 watch_raw 정제 후 RAG 반영.

### ✅ 자동화 목록 통합 표시 (telegram v3.43) ← NEW
- **문제**: "자동작업 목록"이 action_scheduler 항목(뉴스)만 보여주고, 코드 하드코딩 JobQueue 6개(주간스캔×2·월간리밸·장중모니터·1시간신호·리마인더)는 안 떴음 → "왜 다 안 떠?".
- **수정**: list op이 `_full_automation_list()`로 action_scheduler 예약 + `_BUILTIN_JOBS`(시스템 고정 작업, 읽기전용) 합쳐 표시.
- 라우터 list 감지 키워드에 "자동화" 추가("자동화 목록 알려줘"도 인식). 회귀 80 PASS.
- **참고(중요)**: RAG로 답한 "자동화된 작업 목록"은 wiki 계획이라 실제 실행과 다름. 실제 실행 작업의 진실은 "자동작업 목록"(action_schedule.list)이 보여주는 7종.


### ✅ 자연어 봇작업 예약 (action_scheduler v1 · router v3.43 · telegram v3.43) ← NEW
- **요청**: "매일 8시 뉴스 보내" 같은 자연어로 봇이 자기 반복작업을 설정하게(하드코딩 탈피).
- **scripts/action_scheduler.py 신규**: ActionSchedule(action/freq/time/weekday/until/enabled/last_fired) + CRUD +
  is_due(멱등, 같은날 1회) + JSON 영속(data/action_schedules.json). 순수 로직 18 테스트.
- **router v3.43**: `_detect_action_schedule` 결정론 파서(시각 "오전8시/8:00/9시10분", 매일/매주+요일, "6/11까지/YYYY-MM-DD까지",
  목록/삭제/중지) → action_schedule 도구 short-circuit(LLM 미호출). validator + spec. 프롬프트 12559<13000.
- **telegram v3.43**: action_dispatch_job(60초 검사 → due면 실제 봇 실행·전송 + mark_fired) + 도구 분기(add/list/delete/disable)
  + _run_action(news/signal/ipo/quant) + 기본 시드(뉴스 매일08:00~6/11). **하드코딩 news_digest run_daily 제거 → 단일 시스템**.
- **이제 가능**: "매일 8시 IT뉴스 보내 6/11까지", "매주 월 9시10분 공모주 스캔", "자동작업 목록", "자동작업 2 삭제"
  — 코드 수정 없이 텔레그램 말로 시각·주기·종료일 제어.
- **테스트**: action_scheduler 18 + router 8. 회귀 284 PASS.
- **사용자 액션**: 봇 재시작. 기존 일정 #7(가짜 리마인더)·news_digest_last.json 삭제. "자동작업 목록"으로 시드 확인.
- **한계**: 분 단위 임의 주기·"평일만"은 v2. 현재 daily/weekly+시각.


### ✅ 뉴스 다이제스트 30분 스팸 버그 수정 — run_daily 전환 ← NEW
- **버그**: run_repeating(30분 검사)+디스크 플래그 방식이 실환경에서 30분마다 중복 발송(19:28·19:58 실측).
  폴링+플래그는 멀티 재시작/레이스에 취약 → 잘못된 설계였음.
- **수정**: PTB `run_daily(time=08:00 KST)`로 교체. 폴링·플래그 완전 제거 → 하루 정확히 1회. ZoneInfo Asia/Seoul 명시.
  종료일 가드(NEWS_DIGEST_UNTIL=2026-06-11) 유지. news_digest_job 대폭 단순화.
- **사용자 액션**: 봇 재시작 + 기존 단일 프로세스 확인(status PID). 스케줄 리마인더 #7(가짜 뉴스 알림) 삭제.
  잔여 플래그 data/cache/news_digest_last.json 삭제 무방.
- **미해결(사용자 요청)**: "매일 8시 뉴스 보내" 같은 자연어로 **봇이 자기 반복작업을 설정**하는 기능(gemma4 라우터 →
  action-schedule). 현재는 하드코딩. 다음 핵심 빌드 후보 = schedule_bot에 bot_action 타입 추가.


### ✅ 뉴스 다이제스트 정리 — 종료일 + 검사주기 + 일정봇 오해 ← NEW
- **오해 정정**: news_digest_job의 30분은 *검사* 주기일 뿐 발송은 하루 1회(멱등). 크롤링도 08시 이후 1회만. → 검사 10분으로 조정(혼란 완화).
- **종료일 추가**: NEWS_DIGEST_UNTIL="2026-06-11" — 매일 08:00 발송, 6/11까지(포함), 6/12부터 자동 중단.
- **중요 발견(자연어 갭)**: 사용자가 "매일 8시 뉴스 크롤링해서 보내"라고 하면 라우터가 schedule_bot(단순 리마인더)로 처리 →
  실제 크롤링 안 함. schedule 항목은 텍스트 알림일 뿐 봇 액션을 실행하지 않음. 실행은 news_digest_job이 담당.
  → 사용자 일정 #7(리마인더)은 중복이므로 삭제 권장. 
- **다음 단계 후보(핵심)**: "봇이 자기 반복작업을 자연어로 설정"하는 기능 — schedule_bot 항목에 action(bot_call) 타입 추가해
  지정 시각에 실제 봇(news/signal/ipo 등)을 실행·전송하도록. 그래야 "매일 8시 뉴스 보내"가 의도대로 동작.


### ✅ 에이전트 파일 권한 — 읽기 전체 + inbox 쓰기 (agent_bot) ← NEW
- **읽기**: 이미 ALLOWED_ROOTS=[ai-agent, obsidian-vault]로 울트론 내부 read_file/list_files 가능(경로 샌드박스, 그 밖 차단).
- **쓰기(신규)**: 사용자 선택 "inbox만". `write_inbox` 도구 추가 — raw/inbox/<ts>_<title>.md로만 저장 →
  기존 watch_raw가 자동 정제→wiki. 코드(ai-agent)·볼트 일반 경로 쓰기 불가. 제목 sanitize + INBOX_DIR 하위 강제.
- telegram `_setup_agent_tools()`에 register_inbox_tool() 추가.
- 테스트 +4(생성·빈 content·sanitize·등록). 회귀 258 PASS.
- **권한 요약**: 읽기=울트론 전체(샌드박스) / 쓰기=raw/inbox만 / 코드수정·삭제·셸=불가(승인형 v2 후보).


### ✅ agent_bot 자동 위임 (telegram v3.42, 복합 명령) ← NEW
- **요청**: /agent 안 붙여도 복합 명령이 에이전트로 가게.
- `agent_bot.is_compound_command(text)`: 행동 동사 2개↑ + 연결어(그리고/하고/같이/또 등) → True(보수적).
  단일 명령·질문은 False → 기존 라우터 단일 도구로 빠르게.
- telegram handle_text 진입부에서 복합이면 route() 대신 agent_bot.run() 자동 위임(메모리 기록 포함).
- 단일 명령은 종전대로: 평문 → 라우터 → 봇(schedule/news/ipo/...) 자동 분기(이미 동작 중이었음).
- 테스트 +4(복합 감지). 회귀 통과.
- **정책**: "복합 명령만 자동 위임" 선택됨. 오발동 시 _ACTVERB_RE/_CONJ_RE 조정.


### ✅ agent_bot v1 — 로컬 Gemma 범용 오케스트레이션 에이전트 (telegram v3.42) ← NEW
- **요청**: 새 봇 코딩 없이 텔레그램 명령으로 임의 작업 수행("너처럼"). 사용자가 **로컬 Gemma 기반** 선택.
- **scripts/agent_bot.py 신규**: ReAct JSON 액션 루프(라우터와 동일 검증된 Gemma JSON 방식). 도구 레지스트리 +
  step cap(5) + 파싱실패/도구오류/LLM실패 graceful + 관찰 길이 제한.
- **안전(v1 경계)**: 임의 셸/코드 실행·파일 쓰기 **제외**. 읽기 전용 + allowlist(프로젝트 scripts/docs/README,
  vault wiki의 허용 텍스트만 접근, 경로 탈출·숨김 파일·DB 차단). 등록 도구: read_file, list_files, news, signal_scan, ipo_scan, quant_phase, rag_search.
- **telegram v3.42**: `/agent <명령>` 핸들러 + `_setup_agent_tools()`(기존 봇을 에이전트 도구로 등록). /help 갱신.
- **테스트**: test_agent_bot 17(루프·파싱·샌드박스·step cap·예외). 전체 회귀 416 PASS.
- **한계(정직)**: Gemma는 다단계 도구 사용 신뢰도가 중간. 기존 도구 조합·파일 읽기·요약엔 적합, novel 코딩/실행은 약함.
- **사용자 액션**: 봇 재시작 후 텔레그램 "/agent 오늘 신호랑 IT 뉴스 같이 정리해줘" 테스트.
- **v2 후보(승인형 실행)**: python/셸 실행 도구를 화이트리스트+텔레그램 승인 버튼으로 추가 / 파일 쓰기 / 자동 라우팅(복합 명령 자동 에이전트 위임).


### ✅ news_bot v1 — IT/AI 뉴스 다이제스트 (router v3.41 · telegram v3.41) ← NEW
- **요청**: 텔레그램 명령으로 IT 뉴스 추출·요약 전송 + 정시 다이제스트.
- **scripts/news_bot.py 신규**: SOURCES(Anthropic·DeepMind·한경IT RSS + NAVER·삼성 스크래핑) →
  RSS 파서(feedparser 우선, stdlib xml fallback) + bs4 스크래핑(best-effort) → 로컬 LLM 3줄 요약(배치 1콜) →
  롤링 dedup(news_seen.json, 600개) → format_digest. 외부 호출 분리로 hermetic. `--check` 진단 CLI.
- **router v3.41**: news_bot 도구 등록 + `_detect_news` 결정론 단락(LLM 미호출, ipo와 동일 안전망) + validator.
  시스템 프롬프트 12206자 → 한도 테스트 12000→**13000** 상향.
- **telegram v3.41**: news_bot 명령 분기 + `news_digest_job`(30분 검사 → 매일 08:00 1회 멱등 푸시) 등록.
- **테스트**: test_news_bot 19 + router 라우팅 4. 전체 회귀 399 PASS.
- **wiki**: `wiki/학습/IT_뉴스_소스.md` 신규(소스표·동작·운영메모).
- **사용자 액션**:
  1. `pip install feedparser --break-system-packages`
  2. `python scripts/news_bot.py --check` — 소스별 수신 건수 확인(RSS URL 변동 시 SOURCES 보정)
  3. `python scripts/index_wiki.py` + 봇 재시작
  4. 텔레그램 "IT 뉴스 요약해줘" 테스트 / 매일 08:00 자동 푸시
- **검증 필요**: RSS URL 정확성(Anthropic/DeepMind/한경 피드 경로) + NAVER·삼성 스크래핑 셀렉터(첫 실행 --check로 확인).


### ✅ signal_bot v3 — 멀티 타임프레임 필터 ← NEW
- **지침 반영**: 상위 TF가 방향, 하위 TF가 타이밍 → 역추세 신호 억제.
- `compute_trend(daily_closes)` 일봉 20MA 위치+기울기로 up/down/neutral 판정(데이터 부족 시 neutral=통과).
- `apply_mtf_filter(sig, trend)`: 매수성(매수·홀딩유지) 신호 + 일봉 하락 → 억제 / 매도성(매도·경고) + 일봉 상승 → 억제(패닉매도 방지). monitor·neutral은 통과, 통과 신호엔 reason에 "추세확인(📈/📉)" 표기.
- `_fetch_daily_raw`(yfinance 1d) 추가, scan()이 1h 신호 산출 후 일봉 추세로 게이트. MTF_ENABLED 토글.
- yfinance가 4h 미지원이라 상위 TF는 **일봉** 사용(지침 1단계 "일봉 추세 파악"과 일치). 4h resample은 후속 옵션.
- **테스트**: test_signal_bot +10 (총 43). 회귀 380 PASS.


### ✅ signal_bot v2 — 워치리스트 wiki 파싱 ← NEW
- **변경**: 코드에 박혀있던 WATCHLIST를 `핵심_자산배분_포트폴리오.md` 표에서 런타임 파싱(콴텍봇 parse_phase_weights_from_wiki 패턴).
- wiki 표에 **코드 컬럼**(KRX 6자리 또는 yf 심볼) 추가 → 표가 완전한 SSOT. 종목·비중·코드·지표를 wiki만 고치면 봇 반영.
- `parse_watchlist_from_wiki(md)`(순수) + `load_watchlist()`(파싱 실패 시 `_FALLBACK_WATCHLIST` graceful) + scan() 연동.
- strategy는 적용지표 텍스트에서 매핑(MACD/볼린저/StochRSI/모니터). FX는 yf_override.
- **검증**: 실제 wiki 파싱 15종목 정상. test_signal_bot +9 (총 33). 회귀 204 PASS.
- **주의(사용자 데이터)**: 위험자산 표 합계가 65%(명시 70%와 5%p 차이) → 총 95%. 신호 동작엔 무영향이나 비중 확인 권장.


### ✅ 기술적 신호 봇 신규 (signal_bot v1 · telegram v3.40) ← NEW
- **요청**: 핵심 자산배분(위험70/안전30) 종목에만 기술적 분석 매매 신호를 1시간봉으로 텔레그램 실시간 전송.
- **wiki 2종 작성**: `wiki/투자/기술적_분석_활용지침.md`(3대 지표·멀티타임프레임·익절/손절 전략),
  `wiki/투자/핵심_자산배분_포트폴리오.md`(15종목 비중 + 지표 배정 SSOT).
- **scripts/signal_bot.py 신규**: 순수 지표 함수(RSI/StochRSI/MACD/Bollinger) + 자산군별 신호 규칙
  (MACD 0선 / 볼린저 하단·상단·Band Walk / StochRSI 과매도+거래량 골든크로스 / 안전자산 급락 모니터).
  ETF 코드 런타임 해석(pykrx get_etf_ticker_list, 디스크 캐시), 1시간봉은 **yfinance**(`코드.KS`, 선물은 KRW=X/JPYKRW=X).
  외부 호출 분리(_fetch_intraday_raw/_fetch_etf_map_raw)로 hermetic. dedup 유틸 포함.
- **telegram_bot v3.41 · news_bot v1 · agent_bot v1 · action_scheduler v1 · research_bot v1**: `technical_signal_job` — 평일 09:00~15:30 1시간 간격 JobQueue,
  같은 (종목·전략·액션) 신호 당일 1회만(멱등 signal_last.json), 실주문 없음. import + 등록 + 상수 추가.
- **테스트**: tests/test_signal_bot.py 24개(지표·신호규칙·스캔 mock·dedup) PASS. 전체 회귀 324 PASS.
- **사용자 액션 필요**:
  1. `pip install yfinance --break-system-packages` (또는 requirements 재설치)
  2. 봇 재시작 → 장중에 자동 푸시. (yfinance 1시간봉이 한국 ETF에서 실제로 받아지는지 **실환경 확인 필요** —
     일부 ETF는 .KS 심볼/거래량 결측 가능. 결측 시 graceful skip되며, 해당 종목은 daily fallback 등 v2 후보.)
  3. wiki RAG 반영: `python scripts/index_wiki.py` (새 노트 2종 인덱싱 → knowledge_bot이 점수표 대신 신호 로직 안내 가능)
- **v2 후보**: 1h 실측 후 데이터 소스 안정화(yfinance 결측 시 대체), 4시간봉/3·5분봉 타임프레임 추가, 신호→paper 자동 기록.


### ✅ 텔레그램 /analyze 결정론 단락 처리 (router v3.38~v3.39) ← NEW
- **v3.38 발견**: 정규식 보강(v3.37)을 넣어도 실환경에서 knowledge_bot으로 빠짐.
  원인 = 26B 라우터가 깨진 JSON을 뱉으면 route()가 `if action is None` 지점에서
  knowledge_bot으로 **early-return** → 그 뒤에 둔 ipo override가 실행조차 안 됨.
- **v3.39 해결**: `_detect_ipo_analyze(query)`를 route() **맨 앞**으로 옮겨 LLM 호출 전
  단락(short-circuit). 구조화된 "<종목> 공모주 … 매력지수" 질문은 LLM 없이 즉시
  ipo_bot/analyze 반환. 감지 조건 = IPO 단서(공모주/매력지수/IPO/청약) + corp_name 추출 +
  숫자 필드 2개 이상(오발동 방지). corp_name도 정규식 추출(`_extract_ipo_corp_name`).
- **검증**: urlopen을 터뜨려도 ipo_bot 반환(LLM 미호출 증명) + 비IPO 질문은 LLM 경로 유지.
  test_router_mode 누적 +20여 개. 171 PASS(router+ipo+coding).
- **실환경 재확인 필요**: 봇 재시작(PID 변경 확인) 후 매력지수 질문 → 5/5 A++ 뜨는지.

### ✅ 텔레그램 /analyze 결정론 정규식 추출기 (router v3.37)
- **실환경 발견**: 26B 라우터 LLM이 한국어에서 IPO 숫자 필드를 못 뽑음.
  실측 — "경쟁률1200 밴드13000~15000 확정16000 유통38.5% 확약78% 시총800억 미래에셋" 입력에도
  확정요소 1~3/5만 추출(필드 늘릴수록 더 악화). lockup뿐 아니라 analyze 전체가 LLM 의존이라 불안정.
- **수정**: `_extract_ipo_fields_from_query(query)` 정규식 추출기 신규(v3.13 mode override 패턴).
  경쟁률/밴드/확정가/유통비율/확약/시총/주관사 결정론 추출. 주관사는 ipo_bot `_UNDERWRITER_TIER`
  키 재사용(약칭 "미래에셋"도 매칭). route() ipo analyze 분기에서 `args={**llm,**rx}` 정규식 우선 보강.
  확정공모가는 밴드 구간 제거 후 검색(밴드 숫자 오인식 방지).
- **검증(e2e)**: LLM이 corp_name만 줘도 → 정규식 보강 → A++ 91점 5/5 (실질19.0%) 정상.
  test_router_mode +10. 162 PASS(router+ipo+coding). 프롬프트 11997자 유지.
- **실환경 재확인 필요**: 봇 재가동 후 위 문장 입력 → ②20/20·③17/20(실질19.0%)·5/5 A++ 뜨는지.

### ✅ 텔레그램 /analyze lockup 전파 갭 수정 (router v3.36)
- **버그**: `router._validate_ipo_args`의 수치 파라미터 화이트리스트에 `lockup_ratio` 누락
  → 텔레그램에서 "확약 78%"를 말해도 validator가 키를 버려 `analyze_manual(lockup_ratio=...)`에 전달 안 됨.
  CLI(`--lockup`)·`analyze_manual`·`score_float_ratio`는 정상이라 단위 테스트로는 안 잡히던 종류(end-to-end 경로 갭).
- **수정 1**: validator 루프에 `lockup_ratio` 추가 → 통과 + float 캐스팅(비숫자는 조용히 무시).
- **수정 2**: 라우터 시스템 프롬프트 ipo_bot spec/예시에 "확약%→lockup_ratio" 추출 지시 추가.
  프롬프트 12000자 한도 준수(11997자) — 추가분 압축.
- **검증(hermetic)**: validator out에 lockup_ratio 유지 + e2e `ipo_run`:
  확약 없이 79.0 A+ → 확약 78% 91.0 A++ ("실질19.0%") 정상 반영.
- **테스트**: test_router_mode +3 (lockup 유지/비숫자 드롭/프롬프트 언급). 155 PASS (router+ipo+coding).
- **남은 일(실환경)**: 현준님 머신에서 실제 텔레그램 봇으로 자연어 추출(LLM) 최종 확인 필요. 아래 가이드 참조.


### ✅ KIND progcom fetcher + 파서 완전 수정 (v3.32)
- DevTools 실캡처: method=`searchPubofrProgComSub`, forward=`pubofrprogcom_sub`
- 날짜 포맷 수정: `_fmt_date()` YYYYMMDD→YYYY-MM-DD
- `_parse_kind_progcom_html()` 9컬럼 구조 재작성 + `_parse_date_kind()` 신규
- `listing_date < sub_start` 소스 불일치 자동 무효화
- `--raw-payload` CLI 옵션 argparse 연결

### ✅ scan 전체 실환경 검증 (v3.32)
- 38(6건) + KIND캘린더(1건) = 7건 머지 정상
- DART 조회 포함 전체 파이프라인 완주

### ✅ 유통비율 스코어링 개선 — 의무보유확약 반영 (v3.33) ← NEW
- `score_float_ratio(ratio, lockup_ratio=None)` 시그니처 변경
- 보정 공식: `effective = ratio × (1 - lockup_ratio/100 × 0.65)`
  - 0.65 = 기관 배정분이 유통가능물량에서 차지하는 비중 가정치
- `compute_attraction_score()` → `score_float_ratio(item.float_ratio, item.lockup_ratio)` 전달
- `analyze_manual()` → `lockup_ratio` 파라미터 추가 + `IpoItem.lockup_ratio` 연결
- `--lockup` CLI 옵션 추가 (argparse)
- 출력: `(38.5%, 확약78%→실질18.9%)` 형태로 보정 내역 표시

**실환경 검증 결과 (마키나락스)**:
```
개선 전: 유통비율 5/20 (명목 38.5%)  → 총점 72.0점 A
개선 후: 유통비율 17/20 (실질 18.9%) → 총점 84.0점 A+
```
코스닥 역대 최고 확약률(78.2%) 종목이 A+ 등급으로 올바르게 평가됨 ✅

### ✅ 공모가 밴드 보완 — DART 백필 (v3.34) ← NEW
- **문제**: KIND progcom·캘린더 테이블엔 공모가 밴드 컬럼이 없음 (확정공모가만 제공)
  → 38커뮤니케이션 미수록 종목은 band_low/band_high 가 영영 None
- `scan_upcoming`: 밴드 미확정 종목은 수요예측 종료 전이라도 DART 보강 트리거
  (공모 희망가 밴드는 수요예측 이전 증권신고서에 이미 기재됨)
- `enrich_with_dart`: DART 증권신고서 본문에서 offer_band_low/high 보완 + 보완 시 로깅
- `_demand_forecast_done`: 확정공모가(final_price)가 있으면 True
  → KIND progcom이 확정가만 주고 demand_end는 누락하는 케이스 보강
- `format_result`: 확정가 미정 시 밴드 표시 — `공모가 12,000~15,000원(밴드)`
- `analyze_manual`: ②번 줄에 밴드 숫자 표시 `(밴드 13,000~15,000원)`

**test_ipo_bot.py 전면 수정 (v3.26 → v3.34)**:
- 삭제된 `_parse_kind_html` import 제거 → collection 실패(전체 테스트 미실행) 해소
- `_MOCK_38_HTML` 컬럼 순서를 실제 38.co.kr 구조로 교정
  (col1 청약기간 / col2 확정공모가 / col3 공모가범위)
- `TestParseKindHtml` → `TestParseKindProgcom` (공모기업현황 목 HTML, 밴드 None 검증)
- 밴드 백필·표시·_demand_forecast_done 신규 테스트 추가
- **95개 테스트 전부 PASS** (이전: 0개 — import 에러로 collection 실패)

### ✅ paper_ui IPO 탭 연동 — 버그 수정 (paper_ui v3.35) ← NEW
IPO 탭 골격(엔드포인트·HTML·JS)은 v3.29에 있었으나 버그 3종으로 실제 작동 안 함:
- **버그 1 (paper_db)**: `ipo_upsert`/`ipo_close`/`ipo_list`가 `PRAGMA table_info`의
  `d[0]`(컬럼 인덱스 정수)을 키로 써서 **정수키 dict** `{0:..,1:..}` 반환
  → 프론트가 `rec.name`·`rec.grade` 접근 시 전부 undefined.
  → `_row_to_dict(row)` 헬퍼로 교체 (slots·positions 등과 동일 패턴)
- **버그 2 (paper_ui JS)**: `renderIpoScan`·`ipoSubscribe`가 `it.score`를 읽으나
  ipo_bot은 `total_score`를 방출 → 점수 칸 항상 "-". → `it.total_score`로 수정
- **버그 3 (paper_ui JS)**: 청약 등록·결과 입력 시 미정의 함수 `showToast()` 호출
  → `ReferenceError`로 등록 후 목록 갱신 중단. → `toast(msg, true)`로 수정
- `gradeColor`에 A++/A+/? 등급 색 추가 (기존 S/A/B/C만 처리 → A++·A+가 회색이던 문제)
- 스캔 표 공모가 칸: 확정가 미정 시 `band_low~band_high` 표시 (v3.34 밴드 보완 연동)
- `/api/health` version v3.31 → v3.35

**test_paper_ui.py 수정·확장**:
- `client` 픽스처 DB 격리 버그 수정 — `monkeypatch.setattr(DEFAULT_DB_PATH)`만으론
  함수 기본인자(def 시점 캡처)를 못 바꿔 실제 `data/paper.db`를 그대로 쓰고 있었음
  (`tmp_path`가 무용지물) → `_conn`을 fresh_db로 리다이렉트해 진짜 격리
- 낡은 `test_health_version_v3_23`(v3.23 기대, 실제 v3.31로 실패 중) → `test_health_version`
- IPO 엔드포인트 테스트 11개 추가 (records/stats/subscribe/close/scan + HTML sanity)
- **paper_ui 40개 테스트 전부 PASS** (이전 29개 중 1개 버전 불일치로 실패 중이었음)

## 다음 단계 후보

1. **Telegram /analyze 실환경 테스트**: lockup 파라미터 포함 흐름 검증
2. **score_demand 구간 세분화**: 현재 1000~1500 사이 단일 구간(17점) — 실데이터로 튜닝
3. **DART 밴드 정규식 정밀화**: `offer_band_high` 패턴 2종이 일부 증권신고서 형식을
   놓칠 수 있음 — 실제 공시 샘플로 패턴 검증·보강
4. **IPO 탭 실환경 확인**: paper_ui 8080 구동 → 스캔→청약등록→상장결과 흐름 브라우저 점검

## 파일 구조

```
ai-agent/
├── scripts/
│   ├── ipo_bot.py          ← 메인 (v3.33) ✅ 전체 파이프라인 검증 완료
│   ├── telegram_bot.py     ← APScheduler 수정됨, intraday_monitor_job 포함
│   ├── paper_db.py         ← performance_stats() FIFO, record_sell()
│   ├── paper_ui.py         ← 성과 탭 (탭6), /api/health v3.31
│   ├── dart_demand_parser.py
│   └── corp_code_loader.py
├── data/
│   ├── dart_metrics_cache.json
│   └── ipo_samples/
│       ├── 38_raw.html
│       ├── kind_calendar_raw.html ← 정상 (4건)
│       └── kind_progcom_raw.html  ← 정상 (7건)
├── tests/
│   ├── test_ipo_bot.py     ← 95개 테스트 (v3.34: 밴드 보완 + 파서 목 교정)
│   ├── test_paper_db.py    ← TestPerformanceStats 7개 테스트
│   └── ...
└── docs/
    └── NEXT_SESSION.md     ← 이 파일
```

## KIND 파라미터 (확정)

| 엔드포인트 | GET main method | POST list method | forward |
|---|---|---|---|
| pubofrschdl.do | searchPubofrScholMain | searchPubofrScholCalnd | pubofrSchol_sub |
| pubofrprogcom.do | searchPubofrProgComMain | searchPubofrProgComSub | pubofrprogcom_sub |

## 스코어링 로직 요약

| 요소 | 함수 | 만점 | 주요 기준 |
|------|------|------|-----------|
| 수요예측 경쟁률 | score_demand | 20 | ≥1500→20, ≥1000→17, ≥500→14 |
| 밴드 위치 | score_band_position | 20 | 상단초과→20, 상단근접(≥0.9)→16 |
| 유통비율 | score_float_ratio | 20 | 확약 반영 실질비율 기준, ≤15%→20 |
| 주관사 | score_underwriter | 20 | Tier1(5대IB)→20, Tier2→14, Tier3→8 |
| 시총 | score_size | 20 | ≤500억→20, ≤1000억→17, ≤3000억→14 |
