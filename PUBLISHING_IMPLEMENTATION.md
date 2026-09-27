# 검증된 변경의 브랜치·PR 게시

Issue #10은 기존 승인·모델·실행 task를 Git commit, task 브랜치, 완결된 draft PR,
정확한 head의 검사 결과에 연결한다. PR merge와 배포는 실행하지 않는다.

## 연결

설치자는 원래 GitCheckout, 그 workspace에서 완료된 AgentLoop, GitPublisher,
현재 App scope를 관측하는 GitHubPublicationGateway, 최종 결정·미검증·배포·복구·지식
설명을 PublicationCoordinator에 전달한다. 임의 모델 출력으로 이러한 객체를 구성하지 않는다.

```python
from ai_dlc.publishing import GitPublisher, PublicationCoordinator
from ai_dlc.publishing.driver import PublicationDriver
from ai_dlc.github.publishing import GitHubPublicationGateway

git = GitPublisher(git_manager, approved_remote, network_command=installed_git_network_command)
api = GitHubPublicationGateway(github_client, bot_actor_id=app_bot_id, app_id=app_id)
publisher = PublicationCoordinator(loop, checkout, git, api, notes={
    "decisions": final_decisions, "limitations": unverified_work,
    "deployment": deployment_plan, "recovery": recovery_plan,
    "knowledge": adr_kb_explanation, "questions": remaining_questions,
})
driver = PublicationDriver(interval_seconds=30)
driver.register(key, publisher)
# ServiceRuntime(..., agent_driver=agent_driver, publication_driver=driver)
# 또는 publisher.run(key)로 한 task의 게시/효과 재관측을 수행한다.
```

변수는 설치자가 준비하는 의존성이다. HTTPS Git은 설치자 소유 network_command가
없으면 네트워크 시도 전에 PUBLISH_NETWORK_UNCONFIGURED로 차단한다. 이 adapter는
정확히 허용한 remote에 대해 App 자격 증명, CA, DNS/OS egress, timeout을 적용하고
비밀정보 없는 bounded bytes 결과만 반환해야 한다. GitWorkspaceManager의 기존 환경
정리와 hook/filter 차단 계약도 유지해야 한다. 모델/runner workspace에 credential을
넣거나 사용자 전역 credential helper·proxy를 상속하지 않는다. 로컬 remote 허용은 fixture 전용이다.
GitHub PR/check API는 기존 App 인증과 공통 policy transport를 사용한다.

기본 serve의 설치자별 checkout/runtime/network adapter 자동 구성은 아직 제공하지 않는다.
등록된 loop와 publisher는 기존 서비스 scheduler에서 실행하고, 게시 후에도 30초 간격으로
현재 head/base·본문·권한을 재관측한다. publisher와 workflow는 서로 다른 잠금을 사용하므로
Git/HTTP 대기 중에도 stop 명령이 task에 반영된다. service 종료의 취소 신호는 각 효과 전에
확인한다. 진행 중인 원격 push/POST를 원자적으로 취소할 수 있다는 의미는 아니다.

## 검증과 Git 객체

- 게시 직전에 필수 사람 승인, source/config/req/des basis, 실제 실행 journal,
  command별 최종 성공 결과, 읽기 전용 AI 리뷰, 현재 workspace digest를 재확인한다.
- trusted publisher 디렉터리의 빈 bare 저장소에서 원래 base를 다시 fetch한다.
  원래 blob과 tracked manifest가 일치해야 한다. 검증한 파일 bytes를 hash-object로 넣고
  mktree/commit object를 구성한다. checkout, add filter, hook, 사용자 signing 설정을 실행하지 않는다.
- Windows에서는 원래 tracked executable mode를 보존한다. symlink/gitlink나 materialized
  LFS/flattened submodule을 일반 blob으로 자동 변환해 게시하지 않는다. 해당 형태는 별도 지원 전 차단한다.
- branch는 task identity에서 결정한 `aidlc/<repo>/<issue>-<digest>`다. default branch를
  대상으로 할 수 없고 기존 ref를 자동 인수하지 않는다. 최초 생성은 정확한 빈 expected ref lease를
  사용한다. 다른 actor가 먼저 생성했다면 fast-forward 가능 여부와 관계없이 실패한다.
  무조건 force push나 main 직접 push는 없다. 서버의 branch/ruleset 거부를 우회하지 않는다.
- 현재 구현은 task마다 하나의 불변 후보를 게시한다. 사람 코드 수정, base 이동, 새 요구사항,
  본문 변경 이후의 자동 rebase/재push/본문 덮어쓰기는 없다. 기존 PR을 유지하고 사람 조정과
  새 코드 검증·승인이 필요한 blocked 상태로 전환한다.

## Durable effect와 문서

candidate build, push, PR 생성, check 생성·무효화 각각의 intent를 먼저 기록한다.
Git commit/tree, 예상 base/ref, source digest, 요구사항/설계와 승인 원본 basis,
최종 PR 문서, check external ID가 같은 journal에 남는다.

push 응답 유실은 정확한 원격 ref, PR 응답 유실은 task marker·소유 Bot·branch·head·base·최종 본문,
check 응답 유실은 App·external ID·정확한 head·실제 결과를 재관측해 확인한다.
pending인데 해당 효과가 보이지 않으면 자동 재시도하지 않는다. 부재 관측은 미실행 증거가 아니다.
PR 조회에는 closed PR도 포함하며 이미 닫힌 PR을 새 PR로 대체하지 않는다.
최대 100페이지를 초과한 불완전한 조회는 fail closed다.

candidate build 중단은 publisher object 디렉터리와 intent를 남기고 별도 확인을 요구한다.
저널 없이 남은 branch/PR도 자동 인수하지 않는다. 다른 프로세스 재시작 시 동일 state와
원래 GitCheckout manifest로 WorkspaceTools.reopen을 연결해 같은 publisher.run을 사용한다.
서비스 재시작에 등록된 publisher가 없으면 PUBLISH_RUNTIME_UNAVAILABLE을 표시한다.

PR 문서는 templates/pull-request.md의 목적/범위, 인수 조건, 최종 설계/결정,
실제 변경, 검증/미검증, 배포/복구, 관련 ADR/KB/질문 항목을 채운다. 모델의 포괄적 주장을
인수 조건 통과로 추정하지 않고 실제 근거와 사람이 확인할 범위를 명시한다.
ADR/KB 파일 변경은 다른 코드와 함께 최종 검증 tree에 포함되어야 하며 검증 후 추가하지 않는다.
기존 StatusPublisher도 게시 상태와 PR을 투영하며 comment intent/복구 계약을 유지한다.

Check 이름은 AI-DLC source verification이다. JUnit에서 실제 통과가 확인된 경우만 success,
exit-code만 있는 경우는 neutral이며 tests_verified=false를 유지한다. 배포 미실행과
merge 승인 부재를 명시한다. 원격 head/base·본문·정책/승인 basis가 달라지면 승인들을 무효화하고
기존 check를 action_required로 바꾼다. 이 변경도 intent와 응답 유실 재관측을 거친다.
원격 권한 때문에 수정할 수 없으면 불명확한 효과를 기록한 상태로 사람이 조정해야 한다.

원격 read와 write 사이의 경쟁은 완전히 제거할 수 없다. task ref 생성은 lease로 보호하고,
base/PR 본문은 효과 전후 다시 관측해 차이를 기록한다. 다른 ref의 이동이나 GitHub PATCH의
원자적 body compare-and-set까지 보장하지 않는다. 게시된 과거 check는 현재 head/base의 인수 승인이 아니다.

## 검증과 실제 환경 경계

`python -m unittest tests.test_publishing tests.test_github_publishing tests.test_publication_driver -v`
는 실제 로컬 bare remote, scripted model, 실제 WorkflowEngine/WorkspaceTools/합성 subprocess,
GitHub API 대역을 연결한다. 정확한 tree·mode/ADR, push 경쟁, 응답 유실/재시작,
본문/head/base 변경, closed PR, stop, 후보 생성 중단, neutral 검사 등을 검증한다.
전체 unittest/core/execution/workflow 회귀도 함께 실행한다.

이 저장소의 #10 개발 PR에는 실제 변경 commit, 전체 테스트·평가 결과와 미검증 범위를 남긴다.
이 개발용 PR은 Codex 개발 작업으로 생성한다. 라이브 GHES App과 실제 LLM이 설치된 이 프로그램을
사용한 무인 자기 적용 인수라고 보고하지 않는다. 그 첫 업무/인수는 #11이며 실제 RHEL 설치,
자격 증명/egress adapter, App check 쓰기와 모델 품질의 환경별 검증은 별도로 필요하다.

프로토콜 근거: [Git push lease](https://git-scm.com/docs/git-push),
[GitHub check runs](https://docs.github.com/en/rest/checks/runs).
