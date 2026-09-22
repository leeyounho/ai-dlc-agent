# 작업·승인·GitHub 소통 상세 계약 D1

[상세 설계](DETAILED_DESIGN.md)의 상태 엔진 구현 기준이다. 요구사항·설계 승인부터 구현 시작 조건까지 라이브러리와 로컬 demo를 구현했다. 아래 `/aidlc` 명령은 parser/엔진에 연결되어 있으나 실제 GitHub 댓글을 수신·게시하는 기능은 아직 없다. 현재 범위와 후속 항목은 [구현 계약](WORKFLOW_IMPLEMENTATION.md)을 따른다.

## 1. repository 정책

요구사항 승인은 필수다. 나머지 선택은 서로 독립적으로 설정한다.

| 설정 | 값 | 기본값·의미 |
| --- | --- | --- |
| start_policy | explicit / on_approval | explicit: 승인과 개발 시작 분리 |
| design_mode | collaborative / automatic | collaborative: 설계안에 대한 명시적 합의 후 구현 |
| merge_mode | manual / automatic | manual: 사람이 GitHub에서 merge |
| required_human_reviews | 0 이상의 정수 | 1: App 자신의 판단은 사람 리뷰로 계산하지 않음 |
| deployment_approval | manual / automatic | manual: 구체적인 release·환경에 대한 승인 후 배포 |
| parent_approval_covers_children | boolean | true: 승인에 명시된 하위 범위만 중복 승인 생략 |
| release_scope | issue / parent | issue: 개별 결과 배포. 묶음 작업은 parent 선택 가능 |

repo별 기본값이고 조직 필수 정책·GitHub의 실제 규칙보다 약하게 적용할 수 없다. 요구사항 승인 댓글에서 `design=automatic`과 같은 Issue별 선택을 허용한다. 어떤 선택이 금지되는지는 repo profile의 allowed overrides로 정한다. merge·배포의 승인 정책은 일반 댓글로 낮추지 않고 운영자 정책 변경으로 관리한다.

예를 들어 `on_approval + automatic + manual`은 요구사항 승인 후 PR 준비까지 자동 진행하고 사람이 리뷰·머지하는 방식이다. `explicit + collaborative`는 승인 후 별도 시작하고 설계를 함께 고친다. 자동 merge·자동 배포도 구현 대상이며 필수 GHES 보호 규칙과 설정된 리뷰 수는 충족해야 한다.

## 2. 사람의 명령

사람이 새로 등록한 댓글의 첫 번째 비어 있지 않은 줄에서 `/aidlc`로 시작하는 명령 한 개만 해석한다. code block·인용문·Issue 본문·Agent 댓글·다른 bot 댓글 속 명령은 실행하지 않는다. 명령 아래 본문은 설명/변경 요구다. 알 수 없는 인자·중복 인자·오래된 revision은 명시적인 오류 댓글로 응답한다.

| 명령 | 효과 | 권한 역할 |
| --- | --- | --- |
| `/aidlc approve requirements req-0003` | 해당 정리본·분리안 승인 | requirement_approver |
| `/aidlc approve requirements req-0003 design=automatic start=on_approval` | 허용된 Issue 정책 선택과 승인 | requirement_approver + starter |
| `/aidlc start req-0003 design=collaborative` | 유효한 승인 범위에서 시작 | starter |
| `/aidlc approve design des-0002` | 해당 설계 revision 승인 | design_approver |
| `/aidlc amend req-0003` + 본문 | 현재 승인본에 대한 변경 요청; 영향 판단 동안 변경 실행 중단 | contributor |
| `/aidlc stop` | 신규 도구 시작 차단, 실행 중 동작 중단 요청 | operator |
| `/aidlc resume` | 일시 중지된 작업의 근거를 재검증하고 재개 | operator |
| `/aidlc retry run-<id>` | 재시도 가능한 실패를 새 attempt로 시작; 승인 우회 불가 | operator |
| `/aidlc cancel` | 확인 후 취소; 이미 발생한 효과는 조회·기록 | operator |
| `/aidlc approve deployment rel-<id> env=staging digest=sha256:<digest>` | 특정 불변 산출물을 특정 환경에 배포하도록 승인 | deployment_approver |
| `/aidlc rollback deploy-<id>` | 기록된 이전 release로 정의된 복구 수행 | deployment_approver |
| `/aidlc status` | 현재 상태 댓글 링크·다음 행동 응답 | reader |

기본 역할 매핑: reader는 해당 repo 읽기 권한, contributor는 repo write 이상, 나머지는 repo write 이상과 운영자가 지정한 사용자 ID allowlist의 교집합이다. 초기 역할 allowlist가 비어 있으면 권한을 모두에게 열지 않고 설정 필요로 표시한다. 팀 기반 확장은 GHES에서 검증된 membership 조회로 처리한다. username 문자열만으로 권한을 고정하지 않는다.

명령 처리는 원본 comment ID·body digest·작성자 ID·대상 revision으로 식별한다. 승인 수신 및 실제 효과 직전에 현재 원본·권한을 확인한다. 명령 댓글을 편집하여 다른 명령으로 재사용하지 않는다. 편집/삭제된 승인 댓글은 아직 수행하지 않은 단계의 승인을 무효화한다. 이미 발생한 push·merge·배포는 자동 취소되었다고 표시하지 않고 현재 효과를 기록한다.

repository 정책 자체가 on_approval이면 요구사항 승인에 따른 자동 시작은 그 정책이 부여한 권한이다. explicit 정책을 Issue 명령에서 on_approval로 바꾸는 경우에는 allowed override와 starter 권한이 추가로 필요하다. 일반 contributor의 amend는 변경안 제출 권한이며 최종 범위를 스스로 승인하는 권한이 아니다.

일반 대화도 읽고 설계·수정안에 반영한다. 하지만 자연어의 '좋아요'를 승인으로 해석하지 않는다. Agent는 승인 가능한 revision과 복사할 명령을 댓글에 함께 표시한다. PR의 공식 리뷰는 별도의 `/aidlc approve code` 없이 원본 review·head commit·현재 GHES 판정으로 처리한다.

## 3. 단계와 실행 상태

phase와 status는 직교한다. phase는 아래 단계, status는 `queued, running, waiting_human, waiting_dependency, blocked, paused, cancel_requested, cancelled, failed, succeeded` 중 하나다. `succeeded`는 전체 완료에만 사용하고 개별 단계 성공은 event로 남긴다. 승인 대기·중단·차단은 실패와 구분한다.

status 전이 계약: queued → running은 slot과 실행 전제 확보 시, running → queued는 다음 자동 단계/허용된 수정 반복을 준비할 때다. 사람 응답이 필요하면 waiting_human, 선행 작업/부모 release를 기다리면 waiting_dependency, 설정·권한·불명확한 효과 때문에 실행 불가하면 blocked, 예산 내 해결할 수 없는 실행 오류면 failed다. 이 상태들은 새 근거를 확인해야 queued로 돌아간다. stop 완료는 paused, cancel 접수는 cancel_requested이며 실제 중단/효과 조회 후 cancelled다. complete 단계의 필수 결과가 모두 확인될 때만 succeeded이고 terminal인 succeeded/cancelled에서 자동 재실행하지 않는다.

| phase | 진입 시 동작 | 다음 단계의 조건 | 다음 phase |
| --- | --- | --- | --- |
| intake | 접수 원문·작성자·시점·출처 보존 | 대상 repo·Issue 확인 | requirements |
| requirements | 정리본·질문·분리안 게시 | 인수 조건·범위가 판단 가능 | requirement_gate |
| requirement_gate | revision에 대한 사람 승인 확인 | 승인 유효, 선행 조건·시작 정책 만족 | design 또는 coordination |
| design | 규칙/관련 코드 검토, 설계 revision 작성 | automatic은 필수 미결점 없음; collaborative는 설계 승인도 필요 | implementation |
| implementation | 작업 브랜치·파일 수정·테스트 작성·Draft PR | 구현 diff·검증 계획 준비 | verification |
| verification | 검사·JUnit·인수 조건·문서 확인 | 해당 head/tree 검사 충족 | pr_review |
| pr_review | 완결된 PR 본문 게시, 리뷰 반영 | 승인/필수 검사/충돌/최신 근거 충족 | merge |
| merge | manual은 실제 merge 대기, automatic은 조건 재확인 후 merge | 실제 merge commit 확인 | release 또는 waiting_dependency |
| release | merge commit을 깨끗한 작업 공간에서 빌드·테스트·패키징 | 불변 artifact manifest 확보 | deploy |
| deploy | 환경 잠금·현재 상태·정확한 산출물·승인 확인 후 배포 | 실제 배포 완료 조회 | postdeploy |
| postdeploy | 정의된 정상 판정·관측 기간 수행 | 정상이고 전체 인수 조건 충족 | complete |
| complete | 최종 결과 게시·Issue 완료 처리 | status=succeeded | 종료 |
| coordination | parent의 child 관계·의존성·전체 범위 추적 | 하위 결과와 통합 인수 조건 충족 | release 또는 complete |

배포 없는 문서/라이브러리 변경은 승인된 작업 범위에서 `deployment_required=false`를 명시하고 release/package 필요 여부를 기록한다. 이 경우 필요한 검증 뒤 complete로 간다. 배포 설정 누락을 배포 불필요로 간주하지 않는다.

표의 `merge -> waiting_dependency`는 phase=merge, status=waiting_dependency로 남아 parent release 결과를 기다리는 경우다. verification 실패가 한도 안에서 수정 가능하면 implementation으로 돌아간다. 승인 후에도 자동화가 불가능한 미결점은 waiting_human/blocked로 표현한다.

기본 경로:

```text
접수 → 정리 → 요구사항 승인 → 시작 조건 확인
   → 설계(자동 작성 또는 사람 협의) → 구현 ↔ 검증
   → 완결된 PR·리뷰 → 머지 → 릴리스 빌드 → 배포 → 정상 확인 → 완료
```

## 4. 변경·중단·재승인

| 상황 | 처리·무효화 범위 |
| --- | --- |
| 요구사항 승인 전 본문 편집 | 새 원문 revision과 정리본 생성, 이전 자료 보존 |
| 승인 후 요구사항 본문 편집 또는 amend | 신규 효과 중지, 기존 승인 보류, 변경안과 새 req revision 생성; 사람이 새 revision 승인 후 재개 |
| 단순 대화·질문 | 답변·설계 후보에 반영; 자동으로 범위 변경/승인 취소하지 않음 |
| 설계 범위·인수 조건 변경 | requirements로 복귀, 새 요구사항 승인 필요 |
| collaborative 설계의 의미 있는 변경 | 새 design revision, 설계 재승인; 구현·검증 영향 기록 |
| automatic 설계의 범위 내 변경 | 새 design revision과 이유 게시, 정책이 허용하면 자동 계속 |
| PR head 변경 | 이전 코드 검증과 head에 묶인 Agent review readiness 무효화, 필요한 검사·사람 리뷰 재확인 |
| base branch 변경 | 통합 충돌·실행 프로필 영향 확인, 새 통합 결과 검사; 기존 head 단위 검사 결과는 역사로 보존 |
| 설정·규칙 변경 | 새 effective digest를 계산, 관련 계획/승인 재평가; 강제 규칙 축소는 즉시 다음 효과 차단 |
| stop | cancellation epoch 증가, 새 tool dispatch 금지; 프로세스 종료 결과 확인 후 paused |
| cancel | 같은 중단 절차 후 cancelled; PR·브랜치를 자동 삭제하거나 배포 rollback하지 않음 |
| Issue 수동 close | 완료 근거가 없으면 cancelled로 기록, 성공으로 표시하지 않음 |
| PR이 merge 없이 close | blocked, 이유·현재 코드 유지. 필요 시 같은 PR reopen; 새 PR이 필요하면 새 실행 Issue로 재계획 |
| Agent 밖에서 수동 merge | 실제 사실 기록 후 exact merge commit 검증. 관문 위반/근거 부족이면 배포 차단 |

명령은 task lock 안에서 state_revision을 확인해 직렬 처리한다. 도구 dispatch 직전에도 cancellation epoch를 확인한다. 긴 모델 호출 취소는 로컬 응답 소비 중단과 원격 취소 지원 여부를 구분한다. 배포 취소는 실행 adapter가 확인할 수 있는 상태까지 재조정하며 '취소 요청'을 '미배포'로 표시하지 않는다.

resume은 새 승인처럼 동작하지 않는다. cancelled/complete 작업은 자동 재개하지 않으며 후속 변경은 새 Issue로 접수한다. paused/failed 작업만 유효 근거를 재확인해 진행한다. 중복 retry 명령은 현재 활성 run이 있으면 추가 run을 만들지 않는다.

## 5. 작업 분리와 의존성

AI는 분리 이유·각 범위·인수 조건·선행 작업·PR 경계·통합 검증·배포 단위를 정리본에 제안한다. 작업이 작다는 이유만으로 무조건 분리하지 않는다. 독립된 인수/리뷰 단위를 기준으로 한다.

사람이 분리안을 포함한 req revision을 승인하면 child 생성 intent를 기록한다. child 식별용 parent/task marker와 기존 Issue 목록을 확인해 중복 생성을 막는다. 상위 Issue에는 체크리스트와 각 실행 Issue 링크를 둔다. API 지원 시 native sub-issue 관계도 같이 설정한다.

- child의 승인 출처는 parent approval + 정확한 child 범위 digest다. child 변경이 승인된 범위를 벗어나면 해당 child 또는 parent 요구사항을 재승인한다.
- 명시적 시작 정책이면 parent `/aidlc start`가 승인된 child 실행을 허용한다. 각 child에는 실제 시작 근거를 기록한다.
- 의존 관계는 DAG로 관리하고 cycle은 거부한다. 기본은 선행 PR merge 후 그 결과를 포함하는 base에서 시작한다. stacked PR 자동 생성은 D1에 넣지 않는다.
- 병렬 child는 독립 workspace·branch·run을 사용한다. 동일 repo 실행 수 한도는 별도로 적용한다.
- parent는 PR을 만들지 않는다. `release_scope=parent`이면 child들은 merge 후 parent release 대기, parent가 마지막 merge를 포함한 명시적 통합 commit으로 검사·한 번 배포하고 결과를 모든 child에 연결한다.
- `release_scope=issue`이면 각 child가 배포까지 완료되고 parent가 전체 통합 인수를 확인한다. 같은 환경의 배포는 환경 잠금으로 직렬화한다.
- 자동 GitHub closing keyword 때문에 merge 즉시 Issue가 닫히지 않도록 PR 연결은 `Refs #...`를 기본으로 한다. 최종 완료 시에만 Agent가 닫는다. 사용자가 수동으로 닫으면 취소/완료 규칙을 적용한다.

## 6. Issue와 PR의 문서 계약

템플릿은 [templates](templates/requirement.md)에 보관한다. 조직의 기존 PR 템플릿이 있으면 필요한 항목을 보존하고 아래 내용을 통합한다. 실제 정보가 없는 칸을 그럴듯한 문장으로 채우지 않는다.

### 6.1 Issue

원래 사람이 쓴 본문은 수정하지 않는다. Agent 댓글 종류:

1. 현재 상태: 1개를 갱신. phase/status/현재 동작/다음 담당/req·design revision/PR/실행/미결 질문.
2. 요구사항 revision: 목적·원문 링크·범위·인수 조건·가정·질문·분리안·승인 명령.
3. 설계 revision: 변경 구조·계약·대안·검증·배포 계획, 이전 revision 차이, 필요 시 승인 명령.
4. 결정/대화 요약: 대화 source IDs와 열린 질문·결정, 기록 위치 링크.
5. 실행·배포 결과: 실제 검증·artifact·배포/rollback 근거.

모든 tool output을 댓글로 도배하지 않는다. 단계 변경·사람 질문·검토 준비·실패/복구·배포 결과를 게시하고 세부 로그는 인증된 웹 링크로 연결한다. GHES 본문 크기 한도는 capability로 기록하고 긴 결과는 인덱스+분할 댓글로 보존한다.

### 6.2 PR

리뷰 요청 전 다음 항목이 완결되어야 한다.

- 목적과 문제, 승인된 범위·제외 범위·인수 조건.
- 최종 설계와 주요 결정 이유, 변경된 인터페이스/자료 흐름.
- 실제 구현과 관련 파일/commit, 요구사항별 충족 근거.
- 실행한 검사·환경·commit·결과, 미실행/실패/기존 실패 구분.
- 배포·정상 확인·rollback 계획 또는 해당 없음의 근거.
- ADR·KB 변경과 Issue/상하위 작업 링크.

리뷰 준비 조건은 코드 검사 + 문서 완결성 + 질문 해소다. 리뷰 중 수정은 PR 문서도 갱신한다. revision/head/digest를 저장하고 head가 바뀌면 준비 상태를 다시 계산한다. 사람 편집을 보존하기 위해 마지막 읽은 body와 재조회 body가 다르면 병합 제안을 만들고 충돌한 부분을 덮어쓰지 않는다. API가 조건부 body 갱신을 보장하지 않으면 동시 편집 흔적을 후속 조회·이력으로 탐지하고 양쪽 내용을 보존한다.

merge 직전 본문 snapshot과 digest를 로컬에 보존한다. merge 직후 실제 head/merge commit과 최종 본문을 다시 읽어 PR에 `최종 문서 기록` 댓글(필요 시 분할)로 게시하고 Issue에 연결한다. 게시 실패는 publish pending으로 남기며 merge나 테스트를 반복하지 않는다. GitHub 기록도 수정될 수 있으므로 무결성 보존 수단과 동일시하지 않는다.

최종 PR 문서는 merge 시점까지의 결과다. 이후 배포·운영 결과는 Issue와 release/deployment record에 연결한다. PR merge만으로 배포 성공을 선언하지 않는다.

## 7. 전체 완료 조건

실행 Issue는 승인 범위 충족, 실제 검증 통과, 필요한 리뷰·merge, 정확한 release, 필요한 배포/정상 확인, 문서/ADR/KB 처리와 최종 결과 게시까지 완료해야 succeeded가 된다. 실패 배포가 rollback으로 복구됐더라도 새 변경의 배포 성공으로 처리하지 않는다.

운영 이후 발견된 결함·개선은 원 작업의 성공 이력을 지우지 않고 연결된 새 Issue로 관리한다. 명시적으로 설정된 rollback만 별도 운영 명령/정책으로 수행한다. 새로운 코드 수정은 항상 새 요구사항 정리·승인부터 시작한다.
