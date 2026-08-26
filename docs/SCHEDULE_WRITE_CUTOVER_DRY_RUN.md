# 일정 쓰기 전환 readiness / dry-run

> 상태: **운영 전환 완료**. 아래 readiness와 순서는 전환 근거 및 rollback 계약으로 보존한다.

## 쓰기 소유권

일정 DB의 쓰기를 한 프로세스로 단순 통합하지 않는다. 목적이 다른 두 operation 집합을
서로 겹치지 않게 유지한다.

| 소유자 | 허용 operation | 역할 |
|---|---|---|
| `private-data-api` | `schedule.add/delete/complete` | 사용자 요청에 따른 일정 변경 |
| `telegram-schedule-notifier` | `schedule.mark_notified`, `schedule.mark_pre_notified`, `schedule.advance_recurring` | 발송 완료 기록과 반복 일정 진전 |

두 소유자는 같은 Private `assistant.db`를 사용하지만 서로의 operation을 수행하지 않는다.
Telegram의 사용자 mutation 직접 SQLite 경로와 API 실패 시 DB fallback은 목표 상태에서
비활성화한다. 알림 스케줄러는 전환 중에도 중지하거나 Private API로 우회하지 않는다.

## readiness 조건

- fresh·복구 검증된 `assistant.db` backup과 rollback rehearsal
- 일정 API writer, client, executor의 동일 activation permit
- 호출별 사용자 승인
- 사용자 mutation 소유자 정확히 `private-data-api` 1개
- notifier 소유자 정확히 `telegram-schedule-notifier` 1개
- notifier operation allowlist 고정 및 사용자 mutation과 비중첩
- notifier가 같은 DB를 계속 사용
- Telegram 직접 사용자 mutation과 direct DB fallback 비활성
- notifier에서 add/delete/complete 실행 금지

## 고정 cutover 순서

1. fresh assistant backup 검증
2. atomic schedule bundle 설치(실행 중 프로세스는 아직 기존 상태)
3. Telegram을 먼저 재시작해 일정 API consumer·무폴백 상태로 전환
4. Telegram 직접 일정 writer 비활성 확인
5. notifier 내부 writer 재등록 확인
6. Private API를 다음으로 재시작해 일정 writer 활성화
7. 일정 writer route·DB scope 및 direct fallback 부재 확인
8. 일정·알림 상태 정합성 확인

Private API 프로세스 전체를 중지하지 않는다. 운영 중인 watchlist writer는 이 전환과 독립적으로
계속 유지되어야 한다.

## 고정 rollback 순서

1. 이전 watchlist-only bundle 설치
2. Private API를 먼저 재시작해 일정 writer 비활성화
3. 일정 API `404`·watchlist writer 유지 확인
4. Telegram을 다음으로 재시작해 일정 직접 사용자 writer 재개
5. notifier 내부 writer 재등록 확인
6. 일정·알림 상태 정합성 확인

자동 DB 복원은 금지한다. DB 복원은 서비스 rollback만으로 정합성을 회복할 수 없고 사용자가
별도로 승인한 경우에만 수동 절차로 수행한다.

## 운영 전환 결과 (2026-08-26)

- 운영 schedule write capability: 활성
- 운영 schedule write HTTP: 인증된 5개 mutation 경로 활성
- 운영 일정: 5건
- 운영 사전 알림: 4건
- 운영 `private_scoped_*`: 검증 scope version 1 / intent 1 / audit 3
- 운영 notifier: 재시작된 Telegram에서 유지
- fresh clone rollback rehearsal: 완료
- rehearsal 산출물: `/private/tmp/ultron-schedule-rollback-wBbn79` (삭제하지 않고 보존)
- 검증 결과: API write, notifier mark/recurrence, legacy writer 재개, API 결과 보존,
  emergency restore, backup 불변 모두 성공
- 실제 운영 cutover: 완료
- runtime bundle: v1 watchlist 운영 bundle 하위 호환 + v2 schedule 기본 비활성 검증 완료
- 실제 운영 bundle `f0c651775aa4…`: schedule capability `true`
- 영구 backup: `20260826T193342+0900`
- 설치 v2 schedule candidate: `/private/tmp/ultron-schedule-candidate-operational-BKWKGk/`
- 이전 v1 bundle 보존: `/private/tmp/ultron-schedule-cutover-operational-PWTtTX/`
- 무데이터 E2E: 존재하지 않는 event 삭제 `applied/deleted=false`, 재실행 `replayed=true`
- 실제 Telegram E2E: 테스트 일정 add 후 `#8` delete, 양쪽 모두
  `pending→approved→applied`, chat version `0→1→2`
- 실제 일정·알림 데이터: 5건/4건 유지, DB integrity 및 메모리 복구 검증 성공
- 전체 회귀 테스트: `1,484 passed`

일정 write 전환과 실제 사용자 update 양방향 검증은 완료됐다. 다음 Phase 2 쓰기 단위는
아직 직접 DB 경로인 Paper `buy/sell`이며 일정과 동일하게 기본 비활성 저장소·HTTP·client·
consumer·readiness·rollback 순서로 별도 설계한다.
