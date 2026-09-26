# 승인된 구현 Agent 흐름

Issue #9는 기존 WorkflowEngine, ModelSessions, WorkspaceTools,
ExecutionCoordinator를 `ai_dlc.agents.AgentLoop`로 연결한다. 원문 정리,
요구사항 승인, 설계, 코드 수정, 테스트 작성, 실제 명령 검증, 참고용 AI 리뷰가
동일한 FileJournal task와 원본 blob에 기록된다. `ready_for_pr`는 이 단계의
완료 상태이며 PR 생성, merge 승인, 배포 성공을 뜻하지 않는다.

## 신뢰 경계와 연결 방법

설치자가 현재 repository 정책의 WorkflowEngine, 공용 ModelSessions,
별도 task source workspace의 WorkspaceTools, 별도 verification WorkspaceManager,
설치 runner, credential environment, 허용 command ID를 AgentLoop에 전달한다.
선택적인 repository 실행 프로필·knowledge는 같은 확정 source commit이어야 한다.
모델 응답, Issue 본문, 저장소 파일로 이 객체나 네트워크 정책을 구성하지 않는다.

```python
from ai_dlc.agents import AgentDriver, AgentLimits, AgentLoop
from ai_dlc.agents.status import StatusPublisher
from ai_dlc.github.status import GitHubStatusGateway

loop = AgentLoop(workflow, sessions, files, verification_manager, installed_runner,
    environment=credential_environment, limits=AgentLimits(),
    command_ids=("unit_test",), generated_patterns=("target/**",),
    source_commit=checkout.commit, execution_profile=profile, knowledge=knowledge,
    status_publisher=StatusPublisher(store, GitHubStatusGateway(github_client,
                                                              bot_actor_id=app_bot_id)))
# key는 이미 capture_source한 task다. 모든 의존성은 설치자가 준비한다.
loop.start(key, "attempt-one")
driver = AgentDriver()
driver.register(key, loop)
# 기존 ServiceRuntime(..., agent_driver=driver)에 연결하거나 loop.step(key)를 호출한다.
```

위 예의 의존성은 설명용 변수이며 자체 실행 script가 아니다. `AgentDriver`는
기존 공정 scheduler에 한 단계씩 전달하며 task 간 mutable workspace 공유를
거부한다. 모델 동시성은 공용 ModelSessions의 global/provider 한도로 제한한다.
사람 대기는 실행 예산을 소비하지 않고 같은 관측 revision에서 재호출하지 않는다.

기본 `serve` CLI의 자동 source checkout/task runtime 설치와 branch/PR 게시 연결은
#10 범위다. 기본 composition은 여전히 `MODEL_WORKFLOW_UNCONNECTED`를 반환한다.
이유는 loop 미구현이 아니라 설치된 task workspace/runner/driver가 없기 때문이다.
새 loop를 이유로 기본 CLI가 운영 ready를 선언하지 않는다.

## 흐름과 승인

1. 모델이 원문과 명시적 amend 댓글을 읽고 summary, scope, acceptance criteria,
   open questions, 선택적인 split proposals를 만든다. 분리 제안은 Issue를 생성하지 않는다.
2. 질문이 남으면 승인을 거부한다. 현재 req revision의 사람 승인은 항상 필요하다.
   repository와 허용된 Issue override에 따라 explicit start를 기다리거나 승인 후 시작한다.
3. 설계는 승인된 요구사항에 묶인다. collaborative는 현재 des revision의 사람 승인,
   automatic은 열린 질문이 없는 설계를 요구한다. 모델은 승인 tool을 받지 않는다.
4. implementation과 test_generation은 같은 workspace에서 순차 실행한다.
   `list_files`, `read_file`, `search_text`, `apply_patch`, `run_checks`만 제공한다.
   patch는 관측한 전체 workspace/file digest가 일치해야 한다. shell/argv 입력은 없다.
5. 별도 snapshot에서 운영자가 지정한 명령을 실제 실행한다. 결과와 JUnit 판정,
   process 종료 증거, plan/source digest를 다음 모델 입력에 넣는다. 실패한 명령은
   종료 확인과 현재 승인을 다시 검사한 뒤 제한된 repair로 전환한다.
6. 최종 검증 후 동일한 source digest를 읽기 전용 review 모델에 전달한다.
   findings가 있으면 제한된 repair로 돌아간다. 의견이 없어도 사람 승인은 생성하지 않는다.

명시적 `/aidlc amend req-NNNN`은 승인·설계를 무효화한다. 안전하게 종료된 단계는
기존 diff와 사용 예산을 보존하면서 다시 정리하고 새 req 승인을 기다린다.
원문 자체 변경, 정책 변경, 미확정 tool/model 효과는 자동 재개하지 않는다.
설계 질문에 답할 때도 amend 명령으로 변경 근거를 남기고 재승인한다.
현재 Issue/승인 댓글/권한/정책/stop/cancel은 도구 직전과 모델·명령 실행 중 재관측한다.
관측은 polling이므로 원격 권한 변경의 원자적 차단을 보장하지 않는다.

## 근거, 예산과 복구

모델 호출·도구 호출·repair 횟수·active time은 한 task run의 지속 예산이다.
기본 한도는 40회, 100회, 3회, 3600초다. 요구사항 변경이나 재시작으로 초기화하지 않는다.
단계 시작 전에 시간 예산을 예약하고 정상 종료 시 실제 경과 시간으로 정산한다.
중단된 단계는 예약을 보존한다. tool claim을 먼저 기록한 후 효과를 적용하며
중복 ID, 문맥 초과, protocol 오류, 반복 실패, 미확정 모델 응답에서 diff와 근거를
저장하고 blocked가 된다. model session의 가능한 retry도 loop가 임의 재발행하지 않는다.

`WorkspaceTools.reopen`은 변경된 workspace를 읽어 복구 근거를 수집한다.
`loop.recover`는 runner 효과를 재관측하고 현재 diff를 기록한다. 미완료 step을
자동 재실행하지 않으며 운영자가 claim/request/process 근거를 확인해야 한다.
`loop.resume`은 종료된 일시 중지/취소 요청에 한정되고 현재 승인과 pending 효과가
없는지 재검증한다. task cancel은 종결 상태다. 모델·tool 예산 실패는 자동 재개하지 않는다.

StatusPublisher는 최신 req/des revision, 질문·분리안, 사람 승인 원본 댓글 링크,
source/diff digest, 실제 검증, AI 참고 의견, 현재 단계/이유/다음 담당을 한 Bot 댓글에 유지한다.
오래된 amendment/check는 원본 댓글·journal에 남긴다. 요약은 승인 증거를 대체하지 않는다.
Issue updated_at만 바뀌면 원문 승인을 무효화하지 않지만 모든 capture 관측은 보존한다.
본문·제목·작성자·열림 상태가 바뀌면 기존 승인으로 진행할 수 없다.

댓글 publication intent는 요청 전에 저장한다. 응답 유실 후 현재 댓글 본문이
정확히 일치할 때만 확인 처리하고, 불명확한 효과를 재게시하지 않는다.
Bot identity와 repository scope를 확인하고 사람이 수정한 본문은 덮어쓰지 않는다.
최대 100페이지를 조회하며 불완전한 scan에서는 게시를 차단한다. GitHub PATCH에는
원자적 body compare-and-set이 없으므로 재관측과 PATCH 사이의 수정 경쟁은 남는다.
기본 scheduler는 ready_for_pr 작업을 계속 polling하지 않는다. 게시자는 게시 직전
`loop.step`의 현재 gate/digest 검사를 거쳐야 하며 #10에서 Git head까지 묶는다.

## 검증 범위

`python -m unittest tests.test_agent_loop tests.test_github_status -v`는 scripted model,
실제 상태/파일 엔진과 번들 synthetic subprocess를 사용한다. 자동/협의 설계,
사람 gate, amend, stop/cancel/권한 회수/정책 변경, 실제 실패→수정→재검증,
테스트 직후 source 변경, 중복 tool, prompt injection, 예산·문맥 초과,
patch 직후 재시작, 상태 댓글 응답 유실/사람 편집/페이지 처리를 검사한다.
원래 소스의 LF fixture를 실제 수정하고 JUnit 결과를 확인하며 네트워크는 차단한다.

실제 LLM 업무 품질, GHES 쓰기, 실제 repository 빌드/통합 테스트, RHEL runner,
Git branch/PR/merge/deploy는 이 로컬 검증에 포함하지 않는다. command가 report pattern
없이 exit-code만 판정하면 tests_verified=false를 유지한다. 합성 성공은 운영 인수 증거가 아니다.
