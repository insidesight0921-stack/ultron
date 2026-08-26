# Private watchlist 쓰기 전환 runbook

> 상태: 2026-08-26 승인된 운영 전환 완료. 아래 순서는 재전환·rollback 기준으로 보존한다.

## 1. 고정 전제

- 대상은 `storage_paths.py`가 선택한 `data/private/assistant.db` 하나다.
- writer owner는 전환 전 `telegram-direct`, 전환 후 `private-data-api` 하나만 허용한다.
- API writer, Private client, Telegram executor는 단일 atomic bundle로 함께 활성화한다.
- Telegram 사용자 add/remove 메시지가 요청·승인 식별자의 원천이다.
- API write 모드에서는 장애 시 direct SQLite fallback을 사용하지 않는다.
- 자동 DB restore는 금지한다. restore는 별도 장애 판정과 사람 승인이 필요한 비상 절차다.

## 2. 승인 요청 전에 다시 확인할 항목

1. 전체 테스트가 통과하고 작업 트리가 의도한 변경만 포함하는지 확인한다.
2. 전환 직전 운영 `paper.db`, `assistant.db` fresh online backup을 새 snapshot으로 만든다.
3. 새 backup 사본에서 API write → legacy writer 재개 → emergency restore rehearsal을 반복한다.
4. 새 증거로 activation candidate를 빈 700 staging 디렉터리에 exclusive 생성한다.
5. candidate가 API/Telegram runtime loader 양쪽에서 같은 bundle/permit fingerprint로 검증되는지 확인한다.
6. 운영 DB 해시·수정 시각과 8091 mutation route 0개를 다시 확인한다.
7. 사용자에게 변경 파일, 서비스 중단 예상 시간, cutover/rollback 순서와 남은 위험을 제시하고 명시적 승인을 받는다.

## 3. 승인 후 cutover 순서

순서는 바꾸지 않는다.

1. Telegram 서비스를 중지해 legacy 직접 writer를 먼저 제거한다.
2. 검증한 candidate를 고정 private activation 파일로 원자 설치하고 권한 600을 확인한다.
3. `.env`에 단일 `AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH`를 설정한다. 개별 쓰기 플래그는 추가하지 않는다.
4. Private API를 재시작해 단일 writer로 올린다.
5. 인증 status의 `writes_enabled=true`, 다섯 write route, 기존 watchlist 조회를 확인한다.
6. Telegram을 시작해 같은 bundle ID/fingerprint의 client/executor를 구성한다.
7. 승인된 한 종목 add의 pending→approved→applied, 동일 메시지 replay 1회, version 1회 증가를 확인한다.
8. list 결과와 DB 논리 스냅샷을 확인하고 direct fallback이 사용되지 않았음을 로그의 비민감 code로 확인한다.

## 4. 서비스 rollback 순서

데이터 자동 복원 없이 writer 소유권만 되돌리는 기본 rollback이다.

1. Telegram API consumer를 중지한다.
2. Private API writer를 중지한다.
3. bundle 환경키를 비활성화한 뒤 Private API를 read-only 모드로 시작한다.
4. Telegram을 legacy 직접 writer 모드로 시작한다.
5. API에서 적용 완료된 관심종목이 legacy list에서 그대로 보이는지 확인한다.
6. writer가 동시에 둘 이상 존재하지 않음을 확인한다.

## 5. emergency restore

DB 손상이나 검증 불가능한 논리 오류가 확인된 경우에만 별도 승인을 받아 수행한다.

1. Telegram과 Private API writer를 모두 중지한다.
2. 손상 DB를 덮어쓰지 말고 별도 보존한다.
3. 승인된 fresh backup을 새 경로에 복구하고 integrity·schema·table count를 확인한다.
4. 복구 DB와 손상 DB의 차이를 검토하고 복구본을 운영 경로로 바꿀지 별도 승인받는다.
5. read-only API부터 시작해 조회를 확인한 다음 legacy 또는 API writer 중 하나만 선택해 시작한다.

## 6. 현재 상태

- fresh backup: `20260826T005056+0900`, restore 검증 완료
- rollback rehearsal: `/private/tmp/ultron-private-rollback-ud2v2hpo`, 산출물 보존
- activation candidate: `/private/tmp/ultron-private-write-candidate-7jrmas7o/`, 운영 고정 파일에 설치
- atomic bundle: `528fcb275432…`, 파일 600 / private root 700
- `.env`: `AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH` 단일 키 설정, plist에는 시크릿·bundle 값 없음
- Private API: `writes_enabled=true`, 인증 필수 mutation route 5개, 단일 writer owner
- Telegram: 동일 bundle의 client/executor로 실행, direct SQLite write fallback 없음
- 운영 무데이터 검증: `000000` remove applied 후 동일 identity replay, `removed=false`
- DB 결과: watchlist 0건·resource version 0 불변, write intent 1건·audit 3건
- 전체 회귀 테스트: 1,353개 통과

초기 전환에서는 실제 사용자 종목을 테스트 데이터로 넣지 않기 위해 3.7의 add/version 증가 대신
존재하지 않는 종목 remove로 상태 머신과 replay를 검증했다. 이어 사용자가 Telegram에서
`삼성전자(005930)` add와 remove를 차례로 실행했다. 둘 다 pending→approved→applied였고
add는 `created=true`/version 1, remove는 `removed=true`/version 2였다. 최종 watchlist는
0건으로 복귀했으므로 합성 요청과 실제 Telegram 양방향 update 검증이 완료됐다.
