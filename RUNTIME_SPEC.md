# 실행·파일 저장·배포 상세 계약 D1

[상세 설계](DETAILED_DESIGN.md)와 [작업 상태 계약](WORKFLOW_SPEC.md)의 실행 기반이다. 숫자는 변경 가능한 초기 기본값이며 실제 성능·복구 SLA가 아니다.

파일 저널·OS instance lock·task CAS·snapshot 복구와 command intent/실행·중단·inspect 기반 복구, service 설정/readiness와 공통 HTTPS transport를 구현했다. 실제 경로는 instance/repository 복합 hash를 사용하며 sources/revisions는 content-addressed blob으로 통합한다. 현재 동기 core의 task lock은 thread RLock이며 async 서비스 통합은 후속이다. inbox·GitHub/LLM adapter·배포 effect intent/inspect·운영 launcher의 OS 프로세스 재조회는 아직 미구현이다. [작업 엔진](WORKFLOW_IMPLEMENTATION.md), [실행 구현 범위](EXECUTION_IMPLEMENTATION.md), [transport 구현 범위](TRANSPORT_IMPLEMENTATION.md)를 함께 읽는다.

다중 모델 D2의 [MULTI_MODEL_SPEC.md](MULTI_MODEL_SPEC.md)를 함께 적용한다. run 기록에 목적별 model/provider/session을 연결하고 모델/도구 호출·활성 시간 예산은 모든 모델을 합산한다. 서비스와 provider 동시성 한도를 모두 적용하며 모델 교체로 도구를 중복 실행하지 않는다.

## 1. 파일 구조와 진실의 기준

```text
state/
  instance.lock
  instance.json                         # instance ID, schema·앱 버전
  inbox/<delivery-key>.json              # 서명 검증한 수신 이벤트
  inbox-processed/<delivery-key>.json    # 처리 결과·task journal 참조
  tasks/<repo-id>/<issue>/
    journal/00000000000000000001.json    # 순서가 있는 commit record
    snapshot.json                       # journal에서 복구 가능한 현재 상태
    revisions/req-0001.json
    revisions/des-0001.json
    sources/<digest>.json               # 원문/명령/규칙의 관측본
    effects/<effect-id>.json             # intent의 재구성 가능한 색인
  runs/<run-id>/manifest.json
  runs/<run-id>/tools/<invocation-id>.json
  releases/<release-id>/manifest.json
  deployments/<deployment-id>/record.json
  capabilities/<instance-or-runtime>.json
  quarantine/                           # 손상·불일치 자료, 자동 실행 금지
logs/<run-id>/<stream-id>.log
artifacts/<release-id>/<digest>/<name>
workspaces/<repo-id>/<issue>/<run-id>/
```

작업 경로에는 검증한 ID만 사용한다. Issue 제목·사용자 경로·브랜치명을 그대로 디렉터리명으로 쓰지 않는다. secret은 이 구조에 넣지 않고 별도 운영 설정 참조로 읽는다. state와 artifacts는 runner 계정에 공개하지 않는다.

현재 GitHub 객체·실행 결과는 외부 사실, journal은 Agent가 관측·판단·실행 의도를 확정한 로컬 기록이다. snapshot·검색 색인·현재 상태 댓글은 파생 자료다. GitHub가 최신 승인 취소를 보여주면 오래된 journal 승인으로 계속 실행하지 않는다.

### 1.1 원자적 로컬 전이

`commit(expected_revision, event, new_state, intents)`:

1. task lock 획득, 최신 revision 확인. 불일치면 최신 사실로 재계산.
2. 참조되는 원문/revision blob을 임시 파일 → flush/fsync → 같은 filesystem rename → 디렉터리 fsync로 먼저 저장.
3. 이전 record digest, event ID, state_revision, new_state, 새 effect intents와 완료 결과를 **하나의 journal record**로 구성.
4. 같은 journal 디렉터리에 임시 파일 저장·fsync하고 다음 sequence 이름으로 교체 없이 확정, 디렉터리 fsync. 이 시점이 로컬 commit이다.
5. snapshot·effect 색인을 temp+atomic replace로 갱신. 여기서 실패해도 journal에서 재구성.
6. commit된 pending intent만 dispatcher가 수행. effect 결과는 다음 journal 전이로 기록.

여러 JSON 파일 교체를 하나의 트랜잭션처럼 취급하지 않는다. journal record가 큰 첨부를 직접 담지 않고 digest 참조를 사용하되, 참조 자료는 commit 전에 영속화되어야 한다. 임시/고아 파일은 실행 근거로 사용하지 않으며 검증 후 정리한다.

로컬 filesystem의 rename·fsync·locking 보장을 시험한다. 공유 NFS를 기본 저장소로 사용하지 않는다. journal sequence의 hole/digest 불일치/손상은 해당 task를 격리하고 자동 진행하지 않는다. fsync 자체로 host/storage 장애에서 무손실을 보장한다고 주장하지 않는다.

### 1.2 잠금과 동시성

OS file lock을 프로세스 수명 동안 유지하여 state directory 소유 인스턴스를 하나로 제한한다. PID 파일만 보고 소유권을 판단하지 않는다. FastAPI worker 수는 1이다.

- task mutation은 task별 asyncio lock 아래서 처리.
- 동일 task에 활성 mutating run 1개. 긴 모델/빌드 호출 동안 task lock은 풀되 실행 전후 revision·cancellation epoch를 확인.
- repo별 merge lock, 공유 deployment target별 환경 lock. 환경 잠금 key는 repo명이 아니라 운영자가 지정한 실제 대상 ID이므로 repo 간 공유 환경 충돌도 통제.
- 초기값: 전체 run 2, repo당 run 1, 모델 요청 2. repo별 round-robin + 내부 FIFO로 한 repo의 점유를 방지.
- waiting_human/blocked/paused는 실행 slot과 모델 프로세스를 점유하지 않음.

local lock만으로 사람이 직접 하는 외부 배포를 막을 수 없다. 배포 직전 generation/현재 release를 검사하고 가능하면 대상 도구의 compare-and-set·환경 잠금을 사용한다. 이를 제공하지 않으면 외부 변경과의 충돌을 탐지하고 uncertain으로 중단한다.

## 2. 외부 효과와 장애 복구

`effect_id`는 operation UUID, 별도의 natural key는 task + effect type + target revision/head/environment다. 재시도는 같은 effect_id를 사용하고 실제 새 작업만 새 ID를 만든다.

| 효과 | 재시도 전에 확인할 사실 |
| --- | --- |
| 모델의 파일 변경 도구 | ToolInvocation과 수정 전/후 파일 digest. 이미 원하는 결과면 결과만 복원, 불일치면 자동 재적용 금지 |
| 빌드/테스트 프로세스 | run/process group 식별자·존속·관측 결과. 이전 프로세스가 끝나지 않았으면 새 실행 금지 |
| Issue/상태/설계 댓글 | App 작성자·등록한 remote ID·body marker·digest. 목록 조회로 존재 확인 |
| child Issue | parent/task marker와 기존 child 연결 |
| push | 원격 작업 ref가 expected old commit 또는 원하는 new commit인지 |
| PR 생성 | repo + 정확한 head/base + task marker; 이미 있으면 연결 |
| PR 본문 변경 | 현재 body digest와 마지막 관측값; 사람 편집 충돌 여부 |
| merge | 현재 PR merged 여부, head, 실제 merge commit |
| 배포/rollback | 대상 adapter의 operation ID 또는 release/digest·generation 조회 |

요청이 timeout됐다는 이유로 효과가 없다고 판단하지 않는다. 결과가 불명확하면 `uncertain` 상태로 inspect한다. inspect로도 판정 불가하면 blocked + 사람 판단이다. 배포 도구에 조회/idempotency 능력이 없으면 D1의 자동 복구 요건을 충족하지 못하므로 해당 환경의 운영 자동화를 허용하지 않는다.

재시작 절차:

1. instance lock·schema·설정·필요 파일 권한·disk 상태 확인.
2. journal 검증 및 snapshot/effect 색인 재구성, 미처리 inbox 대기열 구성.
3. 실행 UID pool·OS process group/cgroup·run marker 조회. PID 숫자만으로 다른 프로세스를 종료하지 않음.
4. 이전 실행 프로세스의 실제 존속/결과를 확인. 코드/테스트 작업의 결과를 수집하거나 확실하게 종료 후 새 attempt를 준비.
5. pending/uncertain remote effect를 inspect. 배포 중이면 reconcile이 우선이며 재배포하지 않음.
6. 최신 GitHub 요구사항·승인·PR·규칙을 재조회하여 유효 작업만 재개.
7. readiness와 웹 snapshot을 갱신. 일부 task 손상은 해당 task만 격리하고 전체를 성공 상태로 만들지 않음.

disk write 실패 시 새 모델/코드/게시/배포 효과를 시작하지 않는다. 현재 효과의 관측은 가능한 범위에서 계속하되 기록이 복구되기 전 성공을 확정하지 않는다. 프로세스 종료 때 신규 intake를 중단하고 처리 중 요청을 drain, 코드 실행을 중단·기록한다. 진행 배포는 확인 가능한 operation ID를 남기며 중단 결과를 다음 시작에서 검사한다.

## 3. Git 작업 공간과 게시

clone/push 인증은 제어 계정의 고정된 Git 실행 경로에서만 사용한다. global/system Git config와 credential helper·hook·filter·임의 URL rewrite를 상속하지 않는다. submodule/LFS는 명시된 내부 remote만 별도 준비한다. 프로젝트가 준 Git config를 제어 계정에서 실행하지 않는다.

1. 현재 허용된 GHES repo의 base ref를 전체 commit으로 resolve.
2. 깨끗한 작업별 checkout 생성. 브랜치 이름은 기존 repo 규칙에 맞추고 없으면 `aidlc/issue-<number>-<task-short-id>` 제안.
3. runner용 복사본을 준비하고 인증 정보 없이 코딩/검증 수행.
4. 변경 파일·tree·추적되지 않은 후보 파일을 수집. `.git`·secret 경로·작업 공간 밖 symlink/path는 게시 대상에서 제외/거부.
5. trusted publishing 공간에 검증된 변경만 적용. repository hook/filter를 실행하지 않는 고정 Git 설정으로 commit 생성.
6. 검사한 source tree와 게시할 tree가 같은지 확인. 빌드가 소스를 바꿨으면 새 변경으로 처리하고 재검증.
7. task의 작업 브랜치만 push. 예상 원격 ref가 달라졌으면 중단하고 사람 변경과 조정. default branch 직접 push·무조건 force push 없음.
8. head가 준비되면 동일 task PR을 생성/갱신. Draft를 지원하면 미완성 상태를 명시.

현재 GitHub 규칙을 다시 확인하고 head 단위 검사 상태를 게시한다. merge가 base를 바꾸는 경우 새 통합 tree 검증과 merge 직전 규칙 조회가 필요하다. merge 직후 release 단계에서 실제 merge commit을 다시 검증하므로 오래된 branch artifact를 배포하지 않는다.

## 4. 컨테이너 없는 코드 실행

제어 서비스는 비-root 전용 계정이다. 설치 시 준비한 실행 UID pool을 run별로 하나씩 빌려 쓰며 서로 다른 UID·home·작업 디렉터리·파일 권한을 적용한다. 승인된 OS 실행 helper는 같은 배포 패키지에 속하는 짧은 subprocess이며 별도 상주 Worker 서비스가 아니다.

helper가 권한을 사용하는 범위는 운영자가 설치한 UID 전환·프로세스 그룹·자원 제한·정해진 경로 설정뿐이다. 임의 username/uid·executable·network rule을 요청으로 받지 않는다. root 소유 manifest의 profile ID와 검증된 task 경로를 받고 환경·supplementary groups를 초기화한 후 권한을 내린다. 서비스에 범용 sudo/shell 권한을 주지 않는다. 정확한 OS primitive와 허용 helper는 RHEL별 설치 시험으로 고정한다.

실행 요청은 `run_id, command_id, typed_arguments, workspace_id, timeout, resource_profile, env_refs`다. command ID는 운영 설정/승인된 execution profile의 argv 배열로 해석한다. shell interpolation을 쓰지 않고 argv로 실행한다. 저장소 script가 필요하면 확인된 경로·digest를 가진 명령으로 실행하며 shell 내용은 여전히 비신뢰 코드다.

- 제어 상태·App 키·LLM 키·다른 작업·운영 배포 credential 접근 금지.
- env는 allowlist로 새로 구성. inherited proxy·HOME·Git config·Maven 옵션을 그대로 전달하지 않음.
- CPU·memory·process count·file size·disk quota/최소 여유 공간을 제한. cgroup과 POSIX limit 기능 차이는 OS profile로 분리.
- 작업 종료 시 전체 process group/cgroup과 남은 파일 소유·열린 프로세스를 확인하고 UID를 재사용. 종료를 확인할 수 없으면 UID를 격리.
- Maven cache는 기본 run별 디렉터리. 공유 쓰기 cache를 기본으로 하지 않음.
- Maven 읽기에 필요한 최소 저장소 credential은 그 빌드 프로세스가 읽을 수밖에 있다. App/배포 credential과 공유하지 않으며 읽기 전용·제한 scope로 공급하고 실행 뒤 폐기한다. 파일 권한만으로 같은 프로세스에서 감출 수 있다고 가정하지 않음.

동적 파일 읽기/patch에도 path canonicalization·symlink race 방지를 적용한다. 실행 중 child가 바꾸는 파일은 수정 전 digest와 안전한 file descriptor 기반 접근으로 검증한다. 파일 탐색 명령을 제어 계정의 shell에서 실행하지 않는다.

## 5. 네트워크 경계

연결 설정의 host 목록은 내부 승인 목록을 표현한다. 기본 outbound transport는 HTTPS/443만 허용한다. 다른 내부 port/비HTTP 목적지가 필요하면 운영자가 별도 runtime egress profile에 scheme/host/address/port/용도를 등록한다. hostname suffix나 사설 IP만 보고 내부라고 판단하지 않는다.

구현된 애플리케이션 transport는 URL·port·redirect·명시적 no-proxy·DNS 결과와 승인 route를 확인하고 TLS/사내 CA를 적용한다. DNS 변경으로 경계를 벗어나면 차단한다. 고정 proxy는 인증·DNS 책임·TLS 방식을 별도 계약으로 추가하기 전 지원하지 않는다. Git·JVM·shell·하위 프로세스는 OS 차원의 UID/목적지 정책 또는 승인된 내부 egress 경로로 통제해야 한다. 기본 거부 정책에서 필요한 목적지만 허용하며 IPv4/IPv6·UDP·DNS·proxy 우회를 포함한다.

runner는 필요한 Maven/fixture 내부 대상만, publisher는 GHES만, 제어 모델 client는 지정된 모델만, deploy executor는 지정 환경만 접근하게 한다. 목적지 policy는 모델이나 repo script가 수정할 수 없다. root/helper 권한으로 egress rule을 동적으로 확대하지 않는다.

프로파일이 production이면 external_access=true·external_hosts·외부 provider는 초기 검증에서 거부한다. test 프로파일이라도 지정 GPT/Maven 외의 목적지로 자동 확장하지 않는다. 테스트 프로파일로 production state/secret을 읽을 수 없도록 설치 디렉터리와 서비스 설정을 분리한다. redirect 허용을 기본으로 하지 않으며 필요한 내부 artifact 경로는 확인된 최종 대상만 등록한다.

운영 차단 시험은 가짜 transport·로컬 listener·격리된 네트워크 fixture에서 수행한다. 실제 공인 목적지로 탐침 packet을 보내며 차단 여부를 시험하지 않는다. 외부 GPT/Maven 통합 시험은 별도의 명시적 테스트 설정에서만 수행한다.

## 6. Maven·JUnit 검증

실행 profile은 JDK 8 경로, Maven binary/version, argv 명령, profiles/modules, JVM 옵션, report patterns와 fixture를 가진다. `pom.xml`·기존 문서·scripts를 읽고 제안하며 예시 명령 하나를 모든 repo에 강제하지 않는다.

운영 실행은 지정 settings.xml, 사용자 홈, local cache, JVM truststore를 사용한다. mirrorOf=`*`로 dependency/plugin repository를 지정 대상에 모으고 wrapper 배포 파일도 내부화한다. mirror가 scripts의 임의 통신을 막는 것은 아니므로 OS 통제를 함께 적용한다. credential은 모델/로그 입력에 넣지 않는다.

검증 순서:

1. 입력 commit/tree·toolchain·설정·명령 digest 기록. 필요하면 변경 전 기준 테스트 수행.
2. 해당 run의 이전 target/report를 사용하지 않는 깨끗한 출력 공간 준비.
3. compile/package/unit test를 execution profile대로 수행. exit code·timeout·signal·stdout/stderr와 시작/종료 시각 수집.
4. 지정 보고서만 크기·개수 제한 아래 읽고 XML DTD/external entity 비활성화. symlink 외부 파일 읽기 금지.
5. suite/test별 실행·실패·오류·skip 수와 경로를 집계. multi-module 집계에서 중복 파일을 제외.
6. exit 0이어도 테스트 0건·보고서 없음·전부 skip은 시험 성공으로 표시하지 않음. aggregator 모듈 0건은 전체 실제 실행 수와 함께 판단.
7. 기존 실패는 별도로 기록하고 새 실패가 없다는 이유로 전체 통과로 바꾸지 않음. 예외 허용은 명시된 repo 정책/사람 결정과 근거 필요.
8. 승인된 인수 조건과 수행한 검증을 연결. unit test로 확인할 수 없는 조건은 미검증으로 남기고 지정 통합/배포 검증을 요구.
9. 최종 source tree 일치 확인 후 결과·artifact manifest 확정.

테스트 코드는 변경 가능한 코드이므로 JUnit 성공만으로 요구사항의 정확성/보안성을 증명하지 않는다. assertion 삭제·대량 skip·검사 설정 약화·인수 조건 누락은 diff 검토 항목으로 표시하고 필수 검사 무력화를 자동 허용하지 않는다.

## 7. 릴리스·배포·운영 피드백

### 7.1 릴리스

실제 merge commit을 깨끗한 release workspace에서 build/test/package한다. 결과는 digest 기반 읽기 전용 artifact 저장소에 확정하고, environment별로 재빌드하지 않고 동일 artifact를 승격한다. 다른 환경 설정은 배포 profile이 공급하며 secret을 artifact에 포함하지 않는다.

manifest에는 merge commit/source tree/toolchain/config/test run/artifact digest를 포함한다. 배포 승인 전에 release ID·환경·변경 내용·정상 판정·이전 release/rollback 가능 여부를 Issue에 제시한다. 승인 후 artifact가 달라지면 승인도 새로 필요하다.

### 7.2 DeploymentPort 구체 계약

| 호출 | 결과·요구 사항 |
| --- | --- |
| plan(release, environment) | target ID, 현재 release/generation, 새 digest, command/adapter revision, verify/rollback 조건 |
| apply(effect_id, plan) | operation_id, accepted/running/succeeded/failed/unknown; idempotency key 전달 |
| inspect(operation_id, target) | 실제 operation 상태·현재 release/digest/generation·관측 시각 |
| verify(target, release) | 지정 probe별 결과, expected identity, 연속 성공/관측 기간 |
| rollback(effect_id, deployment, previous_release) | 새 operation_id, 이전 release에 대한 복구 시도 |
| inspect_rollback(...) | 이전 release가 실제 복구되었는지와 정상 확인 |

첫 구현은 `command` adapter로 정한다. root/운영자 소유의 고정된 command profile을 실행하고 JSON stdin/stdout 계약을 사용한다. repository의 배포 script는 검토·고정된 profile로 등록되기 전 배포 credential을 가진 계정에서 실행하지 않는다. repo 설정으로 임의 shell을 주입하지 않는다. HTTP 등 다른 도구는 같은 port의 adapter로 추가한다.

command 출력만으로 배포 성공을 확정하지 않는다. inspect/verify는 지정된 대상 상태·artifact version을 독립적으로 읽는다. command profile에 idempotency/조회 경로가 없으면 자동 배포 준비 상태가 아니다. 대상 환경을 모르므로 SSH/systemd/WAS 등을 임의로 확정하지 않는다.

### 7.3 환경별 필요한 값

환경 profile은 `schema_version, enabled, environment_id, target_lock_key, adapter, command_profile_id, allowed_endpoints, credential_env, prerequisites, apply_timeout_seconds, inspect_interval_seconds, verify_checks, stabilization_window_seconds, rollback_mode, rollback_profile_id, migration_policy, observation`을 필수로 갖는다. 타입과 조건은 [설정 계약](config/CONFIG_CONTRACT.md) §4를 따르고 실제 값은 운영 환경에서 채운다.

verify_checks에는 probe method/command ID, 기대 HTTP code/내용/실행 결과, 기대 release 식별자, timeout, 연속 성공 횟수를 명시한다. endpoint 하나가 200이라는 이유만으로 모든 앱의 정상 판정을 대신하지 않는다. 누락된 check/관측 기간은 자동 기본값으로 성공 처리하지 않는다.

배포 단계:

1. 환경 lock, 최신 승인·계획·대상 generation·필수 검사 확인.
2. 이전 release/digest와 rollback 계획 저장, durable apply intent 확정.
3. apply 후 inspect로 실제 결과 확인; 통신 timeout이면 uncertain으로 재조정.
4. verify와 stabilization window 통과 시 succeeded 기록·GitHub 게시.
5. 실패 시 rollback_mode=`automatic`이고 사전 정의된 조건을 충족하면 기록된 이전 artifact로 rollback. manual이면 구체적인 복구 명령을 기다림.
6. rollback 성공은 `deployment_failed_recovered`, 실패는 `rollback_failed`, 판정 불가는 `unknown`으로 보존하고 Issue에 표시. 새 기능 배포 완료로 표시하지 않음.

DB migration이 있는 대상 프로젝트는 reversibility/호환성·백업/복원 절차가 있어야 한다. irreversible 단계가 있으면 코드 rollback만으로 복구 가능하다고 보고하지 않는다. Agent 자체에 DB를 도입한다는 뜻은 아니다.

### 7.4 운영 피드백

각 환경의 동일 내부 상태 probe 또는 승인된 운영 adapter로 지정 주기 관측한다. 기본 제공 방식은 내부 HTTP health/version과 고정 command probe이며 외부 모니터링 서비스는 요구하지 않는다. 관측 실패·인증 오류는 앱 장애와 구분한다.

설정된 횟수/기간을 넘는 동일 incident는 environment+check+release로 중복 억제하여 한 운영 이벤트와 후속 Issue에 연결한다. source event·영향·관련 release·실제 근거를 첨부하고 요구사항 정리/승인부터 시작한다. 모델이 분석 문장만으로 장애 해결·새 코드 배포를 실행하지 않는다. 사전 승인된 rollback 정책은 위 절차로 수행할 수 있다.

## 8. 초기 한도와 오류 분류

| 항목 | 기본값 |
| --- | --- |
| 전체/repo 동시 run | 2 / 1 |
| run 모델 호출 수 | 80 |
| run tool 호출 수 | 200 |
| 구현·검증 수정 반복 | 3 |
| 단일 모델 응답 대기 | 120초 |
| 단일 빌드/테스트 | 1,800초 |
| run 활성 실행 시간 | 7,200초, 사람 대기 시간 제외 |
| transient read 재시도 | 최대 3회, 1/2/4초+jitter, 서버 제한 시간 존중 |
| command 종료 유예 | 10초 후 강제 종료 시도·결과 확인 |
| run 로그 cap | 100 MiB, 이후 잘림 표시 및 핵심 결과 별도 보존 |

`AUTH/CONFIG/POLICY_DENIED`는 자동 재시도하지 않는다. `RATE_LIMIT/TRANSIENT_READ`는 한도 안 재시도. `MODEL_PROTOCOL`은 제한된 교정. `BUILD/TEST_FAILED`는 승인 범위·예산 안 수정. `EFFECT_UNKNOWN`은 inspect만. `STATE_CORRUPT/ISOLATION_UNAVAILABLE/DISK_UNAVAILABLE`는 해당 실행 차단. 자동 재시도의 대기는 scheduler를 막는 sleep이 아니며 다른 작업은 계속 처리한다.

한도 도달 시 checkpoint·현재 diff·실패 근거·추가 작업을 게시한다. 사람 retry는 새 attempt와 명시적인 추가 예산 기록을 만든다. 같은 run의 예산을 몰래 초기화하지 않는다.

## 9. 설치·업데이트·백업

RHEL별 패키지는 runtime, application, native libs, launcher, web assets, dependency lock, license manifest, checksum/signature manifest, systemd unit template, 설정 schema를 포함한다. 실행 시 pip/npm/public update가 필요하지 않아야 한다. 사내 Maven의 고정 artifact에서 설치하며 좌표·CPU·인증서·toolchain 경로는 실제 환경에서 확정한다.

`ai-dlc validate-config`는 네트워크 없이 schema·참조·경로·프로파일 제약을 검증한다. `ai-dlc doctor --local`은 filesystem/runtime/격리 기능을 로컬에서 확인한다. `doctor --internal`은 명시된 내부 endpoint만 계약 점검한다. 진단 실패가 다른 endpoint 자동 탐색으로 이어지지 않는다.

업데이트는 새 intake/dispatch 중단 → 실행 drain/중단 확인 → state 일관된 백업 → migration dry run → 새 패키지 교체 → migration → doctor/readiness → 재개 순서다. 되돌릴 수 없는 state migration을 수행한 뒤 binary만 downgrade하지 않는다. 이전 버전으로 복구할 때는 호환 state 또는 일관된 백업과 GitHub/배포 실제 상태 재조정이 필요하다.

활성 작업의 source/revision/journal과 참조 artifact는 자동 삭제하지 않는다. 완료 로그 기본 보존 후보는 30일, 완료 workspace는 7일이며 운영자가 용량·정책을 확정한다. 원문·승인·최종 PR snapshot·release/deployment 근거의 보존 기간은 별도 운영 정책으로 정하고 미확정 상태에서 자동 purge하지 않는다. 용량 임계값에 도달하면 경고·신규 실행 차단으로 대응한다.

백업은 writer를 일시 정지한 일관된 시점에 checkpoint/journal/revisions/manifests를 같은 묶음으로 복사하고 digest를 확인한다. 대용량 workspace는 복구 가능성에 맞춰 제외할 수 있으나 제외 사실을 manifest에 남긴다. 복원은 격리된 내부 환경에서 시험하며 repo/실행 수·journal replay·불명확한 effect 재조회까지 검증한다.

## 10. 필수 인수 시나리오

아래 시나리오는 [EVALUATION_SPEC.md](EVALUATION_SPEC.md)의 필수 관문에 연결한다. 그 외 실제 업무 성공률·반복 안정성·사람 수습 시간·대표성도 평가하며, 단순히 사례 목록의 실행 여부만으로 운영 적합성을 판정하지 않는다.

1. Java Issue 접수 → 원문 보존/정리 → 승인 → 자동/협의 설계 각각 → 구현 → JUnit → 완결된 PR → 리뷰·merge → release → 배포 → 정상 확인 → 완료.
2. parent/child 생성 응답 유실, 의존성 대기·cycle 거부, child 병렬 수정, parent 단일 release의 중복 배포 방지.
3. 승인 댓글 위조·편집·삭제·권한 회수·과거 revision, 명령 인용문, bot loop를 실행 근거로 쓰지 않음.
4. 승인 후 요구사항 변경, PR head/base 변경, 사람 PR 본문 편집, 외부 수동 merge를 올바르게 재검증.
5. inbox fsync 전후, journal 확정 전후, snapshot 갱신 중 종료 시 상태 복구. 손상/디스크 full에서 실행 차단.
6. push/PR/merge/apply/rollback 응답 유실 후 실제 상태 조회, 중복 효과 생성 방지. 판정 불가하면 차단.
7. Agent·runner 강제 종료, 남은 프로세스, UID 재사용, secret/state/다른 작업 접근, 외부 경로 차단을 로컬 fixture로 검증.
8. Maven 정상/실패/0 tests/skip/오래된 보고서/multi-module/XML 악성 입력, 수정된 source tree와 artifact 불일치 탐지.
9. deploy 성공·timeout·잘못된 version·health 실패·rollback 성공/실패·외부 운영자 충돌·migration 제약.
10. 웹 repo 권한·집계·로그/SSE 노출·session 만료·권한 취소·연결 복구·XSS·외부 자산 자동 요청 부재.
11. RHEL 7·8·9 각각 offline 설치·TLS·Git·Java·실행 격리·웹·재시작·업데이트·복원. 미검증 OS를 지원 완료로 표시하지 않음.
12. 운영 외부 설정 거부와 명시적 테스트 분리. GPT 성공을 Gemma 호환성 성공으로 표시하지 않음.
13. 운영 probe 장애를 앱 장애와 구분하고 incident 중복 Issue를 만들지 않음. 코드 개선은 새 요구사항 승인부터 진행.

원문·승인·설계·실제 코드/검증·배포·복구의 연결과 위 시나리오를 모두 만족해야 해당 환경의 SDLC 전 과정 운영 도입을 완료로 판단한다.
