# 공통 실행·소스 검증 구현

언어에 독립적인 WorkspaceManager, ExecutionPlan, ExecutionCoordinator, RunnerPort와 JUnit 결과 해석기를 구현했다. 요구사항·설계 승인 엔진과 같은 파일 저널을 사용한다. 로컬 demo는 패키지에 고정된 합성 프로그램만 실행한다. 운영 repository 코드는 verified 설치 profile과 제한된 native helper가 준비된 RHEL에서만 `InstalledRunner`가 위임하며, 그 외 환경은 차단한다.

## 1. 실행 흐름

```text
승인된 요구사항·설계
  → 명시적인 로컬 파일 목록으로 별도 source snapshot 생성
  → repository에 등록된 command/toolchain과 ExecutionPlan 일치 확인
  → 최신 승인·원문·권한·epoch 및 runtime preflight 확인
  → reserved intent + 실행 계획 blob을 저널에 확정
  → 실행 직전 다시 승인·source/runtime digest 확인
  → dispatching 기록 → runner.start → identity와 running 기록
  → 실제 종료 관측 → source 재확인·결과 해석 → command 결과 확정
```

Task 전체와 command의 성공을 구분한다. JUnit 명령이 성공해도 Task는 implementation 단계이며 PR·merge·배포·전체 완료를 대신하지 않는다. `verification=exit_code`는 명령 종료만 확인하고 `tests_verified=false`를 명시한다.

## 2. 소스 snapshot

[WorkspaceManager](ai_dlc/execution/workspace.py)는 제어 state 등 지정한 보호 경로와 겹치지 않는 작업 root를 사용한다. source root도 이 경로들과 분리한다. 명시적으로 선택한 파일만 새 workspace의 tree에 복사하고 파일별 경로·크기·SHA-256·실행 bit와 전체 manifest digest를 기록한다. 원본을 수정하지 않는다.

- 외부/장치 경로·상위 경로 이동·`.git`·심볼릭 링크·junction·hard link·특수 파일·Windows 예약 파일명·대소문자 중복을 거부한다.
- 기본 한도는 파일당 8 MiB, 전체 64 MiB, directory entry 10,000개다. 큰 repository에 필요한 한도 구성은 설치 profile 확장 대상이다.
- 승인된 generated_patterns에 속하는 새 출력만 허용하며 원래 source 파일의 변경·삭제·실행 bit 변경을 실패로 처리한다. 허용 패턴이 원래 source의 변경을 허용하지는 않는다.
- source manifest와 plan은 제어 저널의 blob에도 보존한다. runner가 작업 공간의 manifest.json을 바꿨다는 이유로 검증 기준을 바꾸지 않는다.

현재 입력은 **로컬 파일 목록**이다. Git clone/checkout, commit에서 추적 파일·mode를 읽는 과정, branch/push/PR은 아직 구현하지 않았다. 이 source digest를 Git commit/tree ID로 표시하지 않는다. 파일 검사는 프로세스를 실행하지 않는 snapshot 준비 시점과 runner가 종료를 확인한 시점에 수행한다. 공격적인 실행 프로세스에 대한 filesystem 격리를 이 경로 검사만으로 보장하지 않는다.

## 3. 계획·runner 계약

[ExecutionPlan](ai_dlc/execution/ports.py)은 command ID, toolchain ID, executable ID·argv·cwd·timeout·report 패턴, source snapshot, 검증 방식, 허용 생성 파일 패턴을 가진다. `from_document`와 coordinator의 `load_plan`으로 저널의 계획을 복원한다. 재시작 후 메모리에 남아 있는 Python 객체에 의존하지 않는다.

| RunnerPort 메서드 | 계약 |
| --- | --- |
| preflight(plan) | 설치자가 승인한 명령·실행 파일·작업 경로·UID/resource/egress 조건을 검사하고 runtime digest 반환 |
| start(run_id, plan) | shell 문자열이 아닌 승인된 명령을 시작하고 run identity와 handle 반환 |
| handle.poll() | 관측 중이면 None, 확실한 종료이면 ProcessResult. 프로세스 트리 종료 확인 포함 |
| handle.cancel(reason) | 소유한 실행에 중단 요청. 요청 자체를 종료 확인으로 취급하지 않음 |
| inspect(run_id, identity) | 기록된 작업을 조회해 종료 결과 반환. None/예외는 재실행 근거가 아님 |

RunnerPort 구현은 설치된 신뢰 코드다. 모델/Issue가 제공한 `isolated=true` 같은 값으로 운영 실행을 허용하지 않는다. 기본 구현은 RUNNER_UNCONFIGURED로 차단하며 제어 계정에서 임의 shell/subprocess로 대체하지 않는다. 코드에는 범용 `execution run --shell ...` 명령이 없다.

현재 제공하는 [FixtureProcessRunner](ai_dlc/evaluation/process_fixture.py)는 별도의 로컬 평가 의존성이다. 지정한 Python executable로 고정된 합성 프로그램만 실행한다. `-I -S`, shell=False, stdin 차단과 최소 환경을 사용하며 사용자 코드·임의 executable·Python source·URL을 입력으로 받지 않는다. 자식 프로그램은 네트워크를 사용하거나 다른 프로세스를 만들지 않는다. stdout/stderr를 계속 비우며 digest/byte 수를 기록하고 64 KiB를 넘으면 종료한다. 환경의 App/LLM credential·proxy·PATH를 상속하지 않으며 Windows는 필요한 SystemRoot만 전달한다.

이 평가 runner는 OS sandbox가 아니며 임의 Java/Python/C# 코드를 실행하는 용도로 사용하지 않는다. 운영용 [InstalledRunner](ai_dlc/execution/installed_runner.py)는 RHEL UID/resource/toolchain/egress profile, root 소유 파일 digest와 exact argv를 검사하고 같은 패키지의 제한된 native helper에만 start/inspect/cancel을 위임한다. helper의 process-tree/UID 재사용 증명이 없으면 성공을 확정하지 않는다. 세부 계약과 실제 미검증 범위는 [RHEL runner 구현 계약](RHEL_RUNNER_IMPLEMENTATION.md)에 있다.

## 4. 중복·중단·복구

- 같은 run ID와 같은 계획의 재전달은 기록된 결과를 반환하고 다시 시작하지 않는다. 같은 run ID에 다른 계획을 지정하면 거부한다. 반환된 과거 결과의 req/des/source/runtime 기준을 새 작업의 결과로 바꾸지 않는다.
- task당 활성 command 하나만 허용한다. reserved 상태도 다른 command의 실행 조건을 닫는다. 전체/repo별 scheduler 한도는 후속 서비스 구현에서 적용한다.
- 시작 권한은 reserve 때와 dispatch 직전에 다시 확인한다. 실제 실행 중에도 epoch·현재 승인 근거를 확인하고 취소/변경/권한 회수 시 중단한다.
- stop은 실행 중인 command가 끝나기 전에는 Task를 paused로 표시하지 않는다. cancel은 cancel_requested로 남고 실제 종료가 확인된 뒤 cancelled가 된다.
- 오래된 승인·원문·정책에 대한 결과는 stale로 기록한다. repo가 disabled로 바뀌어도 실행 종료 사실은 저널에 남길 수 있다.
- timeout과 종료 요청 후 확인 유예를 분리한다. 확인 유예 기본값은 10초이며 coordinator에서 설정할 수 있다. 종료가 확인되지 않으면 uncertain/blocked다.
- 실행 계획만 reserved로 남은 경우에는 최신 조건을 재확인하고 같은 run을 시작할 수 있다. dispatching/running/uncertain이면 먼저 inspect하며 자동 재실행하지 않는다.
- start 응답 유실, process 관측 실패, 불명확한 프로세스 트리도 uncertain이다. PID만으로 프로세스를 죽이거나 새로운 작업을 실행하지 않는다.
- 완료 관측은 저장했지만 snapshot 갱신에 실패한 경우 등 파일 오류는 기존 store의 unhealthy/복구 규칙을 따른다. 유효한 commit 없이 새 외부 효과를 시작하지 않는다.

원격 GitHub push/PR·merge·배포의 effect reconciliation, retry 예산과 전체 단계 scheduling은 이 command coordinator의 구현 완료 범위에 포함하지 않는다. 설치 runner는 재시작 후 helper identity 조회 계약을 구현했지만 실제 RHEL helper/process 조회 evidence는 아직 없다.

## 5. JUnit 해석

[JUnit collector](ai_dlc/execution/junit.py)는 UTF-8 JUnit testsuite/testsuites 문서를 읽는다. 루트/여러 module의 glob을 합치고 같은 파일이 패턴 여러 개에 맞아도 한 번만 센다. testcase에서 실행·failure·error·skip 수를 계산하고 문서에 적힌 합계와도 비교한다.

보고서는 최대 256개, 개별 2 MiB, 전체 16 MiB로 제한하고 XML depth/node 수를 제한한다. DTD·entity 선언과 외부 참조, 잘못된 합계·문서 형식을 거부한다. 네트워크에 접속해 외부 entity 차단을 시험하지 않는다. 지원하지 않는 JUnit 변형을 임의로 정상 처리하지 않는다.

JUnit 검증은 시작 전 해당 report가 없는 깨끗한 workspace를 요구한다. 기존 보고서를 자동 삭제하거나 그 결과를 현재 실행 성공으로 사용하지 않는다. exit 0이라도 report 없음·testcase 0개·전부 skip·failure/error 존재·source 변동은 통과하지 않는다. 이 검사는 unit test 근거이며 요구사항 의미의 정확성·통합·배포 검증을 대신하지 않는다.

## 6. 실행해 보기

```text
python -m ai_dlc execution demo
python -m unittest discover -s tests -v
```

demo는 합성 Issue의 승인→설계→실제 합성 subprocess→JUnit 검증을 여섯 번 실행한다. 정상, 비정상 종료, 보고서 누락, 테스트 0건, 전부 skip, source 변경을 판정한다. 실패하도록 만든 명령을 실패로 잡아야 그 사례가 통과다. 각 run을 다시 전달해 실제 실행이 한 번뿐인지도 확인한다.

결과는 `var/execution-evaluations/execution-<id>/report.json`과 report.md다. source/suite digest, run ID, 상태·검증 결과, 승인/source/runtime 기준, 재전달 후 실행 횟수를 포함한다. `workflow inspect`에서 해당 state_root와 `local-execution-demo`, repo 1, Issue 1~6으로 같은 Task를 볼 수 있다.

network guard는 제어 Python 프로세스의 socket/DNS 시도를 가로챈다. subprocess는 통신 코드가 없는 고정 프로그램이지만 이 guard로 자식 OS 통신을 격리했다고 주장하지 않는다. 어떤 보고서도 운영 도입을 승인하지 않으며 eligible_for_release=false다.

현재 개발 환경은 Windows/Python 3.12.6이다. 전체 unittest와 합성 execution/core 평가를 실행하지만 이는 RHEL 격리 증거가 아니다. 설치된 Java는 11.0.2이고 Maven은 없으므로 Java 8/Maven fixture는 실행하지 않았다. 실제 native helper와 RHEL 7/8/9, 별도 UID·filesystem·egress·process tree·resource enforcement, wheel 생성/설치는 아직 검증하지 않았다.
