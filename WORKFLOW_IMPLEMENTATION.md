# 작업 상태·승인·복구 구현

현재는 실제 LLM/GHES/실행기를 연결하기 전의 공통 제어 엔진이다. 코드 언어와 무관하며, 원문 보존부터 구현 시작 조건 확인까지 실행 가능한 라이브러리와 로컬 검증 명령을 제공한다. 전체 SDLC 완료나 운영 준비를 선언하지 않는다.

## 1. 구현한 경로

```text
Issue 원문 관측·보존
  → 요구사항 정리본 req revision
  → 필수 사람 승인
  → explicit 시작 / on_approval 자동 시작
  → 설계 des revision
  → collaborative 설계 승인 / automatic 설계
  → implementation/queued: 구현 실행 조건 확인 가능
```

`queued`는 실행 대기이며 실제 코드 수정·테스트 성공을 뜻하지 않는다. `implementation_gate`는 조회 시점의 조건 확인 결과다. [command coordinator](EXECUTION_IMPLEMENTATION.md)를 연결했으며 같은 task lock 아래 최신 revision·cancellation epoch·원본·권한을 다시 확인하고 intent를 확정한 뒤 등록된 runner를 시작한다. 이전 GateCheck를 실행 토큰으로 재사용하지 않는다.

- 정리본은 summary/scope/acceptance_criteria/open_questions, 설계는 summary/changes/validation_plan/open_questions를 갖는다. 미결 질문이 있으면 승인·자동 구현 진행을 막는다.
- 원본 Issue의 제목·본문·작성자·source version과 각 관측본을 보존한다. 문자열의 공백·개행을 정리해서 덮어쓰지 않는다. 원문 body의 UTF-8 digest와 구조화한 관측본 digest를 따로 기록한다.
- 요구사항 revision을 다시 만들거나 원문·정책이 바뀌면 요구사항 승인·시작·설계 승인을 무효화한다. 과거 원문·문서·승인은 저널과 blob에 남는다.
- 승인·시작은 현재 revision에만 적용된다. collaborative 설계를 수정하면 새 des revision에 다시 승인받는다.
- 부모/자식 Issue 범위 승인과 의존성 DAG, 실행 attempt·retry, 구현 이후 검증·PR·merge·release·deploy 단계는 후속 구현이다.

## 2. 승인과 외부 사실의 경계

[ObservationGateway](ai_dlc/workflow/observations.py)는 신뢰된 GHES 연결부가 구현할 인터페이스다. 실제 네트워크 구현은 아직 없다.

| 조회 | 연결부가 제공해야 하는 사실 |
| --- | --- |
| issue(task) | 해당 instance/repo/Issue의 현재 원문·작성자·version·open 상태 |
| comment(task, comment_id) | 현재 원본 댓글·사람/bot 종류·작성자 ID·생성/수정 시점. 삭제됐다면 None |
| permission(task, actor_id) | 현재 repository 권한. 조회 실패를 write로 바꾸지 않음 |

Webhook payload나 모델이 `verified=true`라고 적었다는 이유로 이 인터페이스를 구현해서는 안 된다. GHES 어댑터는 원본을 다시 조회하고 실제 권한을 확인해야 한다. 라이브러리의 Python 호출자는 신뢰된 제어 프로그램이다. CLI에는 임의 사용자 ID로 운영 승인을 만드는 명령을 제공하지 않는다.

명령은 사람이 작성한 미편집 댓글의 첫 비어 있지 않은 줄에 있어야 한다. 인용문·code fence·들여쓰기 code block·bot 명령을 거부한다. 현재 지원하는 변경 명령은 requirements/design 승인, start, amend, stop, resume, cancel이다. status 조회는 별도 reader의 책임이며 CLI `workflow inspect`가 로컬 상태를 보여준다. GitHub 댓글 응답과 게시 기능은 아직 없다.

일반 contributor의 amend를 제외한 변경 명령은 repo write 이상과 역할별 사용자 ID 목록을 모두 충족해야 한다. explicit → on_approval 선택에는 starter 역할도 필요하다. 저장소 자체가 on_approval이면 그 정책에 따라 요구사항 승인 후 시작한다.

구현 조건 확인 때 원문·승인 댓글·현재 권한을 다시 조회한다. 삭제/편집/권한 회수는 즉시 gate를 닫고 `reconcile`로 영속 상태에도 반영한다. 원문 편집은 `capture_source`, 운영자 설정 변경은 `refresh_policy` 후 정리·재승인을 거친다. 조회 실패도 진행 근거로 쓰지 않는다.

stop/cancel은 epoch를 올리고 새 진행 조건을 닫는다. 활성 command가 없으면 바로 paused/cancelled가 되며, 실행 중이면 command coordinator가 종료를 확인한 뒤 확정한다. cancel은 그동안 cancel_requested이고 결과가 불명확하면 재실행을 차단한다. resume은 승인을 새로 만들지 않고 재검증하며 cancelled는 재개할 수 없다. 수동 Issue close는 성공 근거가 아니므로 실행 중단 확인 후 cancelled로 기록한다.

## 3. 파일 저널

[FileJournal](ai_dlc/storage/journal.py)은 운영자 소유의 로컬 디스크를 전제로 한다. DB나 외부 서비스를 사용하지 않는다.

```text
state/
  instance.lock
  instance.json
  tasks/<hash(instance_id, repository_id)>/<issue_number>/
    journal/00000000000000000001.json
    snapshot.json
    blobs/<sha256>.json
```

소스·revision을 각기 다른 파일 종류로 복제하는 대신 immutable JSON blob에 보관하고 저널에서 digest로 참조한다. 이 경로는 기존 런타임 설계의 sources/revisions 영역을 통합한 구현이다. repo 이름 변경과 여러 GHES의 동일 repository ID가 충돌하지 않는다.

1. 프로세스 수명의 OS 파일 잠금으로 같은 root의 두 인스턴스를 차단한다. POSIX flock, Windows msvcrt를 사용한다.
2. task별 thread lock 안에서 expected_revision을 비교한다. 다른 전이가 먼저 진행되면 STATE_CONFLICT다.
3. blob을 먼저 flush/fsync한 뒤 같은 디렉터리의 임시 파일을 hard link로 확정한다. 기존 파일을 덮어쓰지 않는다.
4. event ID·요청 digest·이전 record digest·새 상태·blob 참조를 한 저널 record로 확정한다. POSIX에서는 디렉터리도 fsync한다.
5. snapshot은 atomic replace로 갱신하는 파생 자료다. 손상/누락되어도 저널 전체를 검증해서 복구한다. 구현 gate는 복구 가능성과 최신 관측을 확인한다.

동일 event ID·동일 내용은 재실행하지 않고 **현재 상태**를 반환한다. 같은 ID의 다른 내용은 EVENT_COLLISION이다. 예전 승인 이벤트가 재전달되어도 새 요구사항의 승인을 되살리지 않는다.

저널 sequence 누락·digest 불일치·blob 손상은 해당 task를 차단한다. `.pending-*`·고아 blob은 확정된 실행 근거가 아니다. 자동으로 손상 이력을 삭제하거나 건너뛰지 않는다. 다른 task의 정상 기록은 읽을 수 있다.

쓰기 실패 시 store를 unhealthy로 두고 추가 commit/gate를 차단한다. 저널 확정 뒤 snapshot만 실패했다면 `Commit.snapshot_current=false`로 확정 사실과 실패를 함께 반환한다. 프로세스를 다시 열고 저널을 복구해야 한다. 응답 유실 후에는 같은 event ID를 재사용한다.

해시 사슬은 디스크 손상·부분 변경을 감지하기 위한 것이며 관리자에 대한 공증 수단은 아니다. 심볼릭 링크·junction을 거부하지만 공격자가 state 디렉터리를 동시에 바꾸는 환경을 지원하지 않는다. runner는 이 디렉터리에 접근할 수 없어야 한다. 공유 NFS, Windows 전원 장애 시의 디렉터리 영속성, RHEL별 filesystem/locking 검증은 완료하지 않았다.

## 4. 로컬 실행과 검증

```text
python -m ai_dlc workflow demo
python -m ai_dlc workflow inspect --state-root <출력된-state_root> --instance local-workflow-demo --repository-id 1 --issue 1
python -m unittest discover -s tests -v
```

demo는 합성 관측자·사람·요구사항·설계로 **실제 상태 엔진과 저널**을 실행한다. 모델이 요구사항이나 설계를 작성한 결과는 아니다. 각 실행은 `var/workflow-evaluations/workflow-<id>/`에 독립 state와 report.json을 남긴다. 원문 → 승인 → 시작 → 설계 승인 → 중지/재개 → reopen 복구 → 원문 변경 → 구 승인 거부를 확인한다. 마지막 상태는 새 요구사항의 승인 대기다.

테스트는 추가로 승인 댓글 삭제/편집·권한 회수·정책 변경·동시 CAS·두 프로세스 잠금·실제 프로세스 강제 종료·저널 확정 전후 실패·손상·snapshot 복구를 확인한다. Python socket/DNS 차단 아래 workflow를 검증하며 외부 주소에 연결해 차단을 시험하지 않는다. OS egress 검증이나 실제 GHES 인증 시험은 아니다.

이전 작업 엔진 묶음의 Windows/Python 3.14 검증 결과는 unittest 76개 중 75개 통과, 심볼릭 링크 생성 권한이 없어 1개 생략이었다. core 계약 평가 33/33과 workflow demo의 10개 상태 전이·8개 확인이 통과했고 intercepted_network_attempts는 0이었다. 260자를 넘는 로컬 경로의 저장·재시작 복구도 시험했다. 이후 추가한 command 실행 검증은 [실행 구현 계약](EXECUTION_IMPLEMENTATION.md)을 따른다. 실제 Python 3.12/RHEL 실행과 전체 운영 평가는 미검증이다.

## 5. 언어 중립적인 실행 설정

repository schema 2의 `project.adapter`에 `command`를 추가했다. 기존 `java_maven` 설정을 그대로 읽는다. immutable ProjectProfile/CommandProfile에 executable_id·argv·cwd·timeout·report 패턴을 보존한다.

- command에서는 JUnit 패턴이 빈 배열이어도 된다. Maven profile은 기존처럼 JUnit 패턴을 요구한다.
- 빈 command_overrides는 이후 repo 규칙에서 명령을 발견해야 한다는 뜻이다. 형식 통과를 실행 준비로 처리하지 않는다.
- executable_id는 설치자가 제공할 toolchain registry에 연결한다. 현재 실제 subprocess 시험은 고정된 합성 runner만 사용하며 repository 코드용 runtime을 자동 탐색/실행하지 않는다.
- [이 저장소의 예시](config/repository.self.example.json)는 unittest와 로컬 평가 명령을 보여준다. timeout은 조정할 예시값이고, 패키징·설치·배포 검증은 아직 포함하지 않는다. enabled=false를 유지한다.

로컬 source snapshot·command intent·실행/중단/복구·JUnit 검증을 이 제어 엔진에 연결했다. 다음에는 Git 기반 source 준비와 모델의 파일 도구, 실제 GHES/LLM 연결 및 운영 runtime을 구현한다.
