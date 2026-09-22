# 이 저장소에 AI-DLC 적용하기

상태: 자기 적용의 개발 순서와 첫 업무 초안. 현재 프로그램이 GitHub Issue를 처리하거나 자신을 수정·배포할 수 있다는 의미는 아니다. 실제 GHES/LLM 연결·원격 Issue 생성은 이번 작업에서 수행하지 않는다.

## 1. 현재 가능한 것과 남은 것

현재는 연결/repository 설정 로더, 여러 LLM 선택, 호출 전 조건 검사, 요구사항·설계 revision·사람 승인·상태 전이·저널 복구와 로컬 평가가 구현되어 있다. GitHub App 설치는 repo 인증·권한·이벤트의 연결점이며 아래 실행 기능을 대신하지 않는다.

| 기능 | 현재 상태 | 자기 적용에 필요한 결과 |
| --- | --- | --- |
| 설정·모델 선택·로컬 평가 | 구현됨 | 현재 코드를 공통 기반으로 재사용 |
| 요구사항 revision·승인·상태·복구 | 구현 시작 조건까지 구현 | 실제 GHES 관측·실행 중단/복구와 연결 필요 |
| GHES App/webhook/API | 미구현 | Issue/댓글 수신, App 인증, 작업 branch/PR 게시 |
| 실제 모델·도구 loop | 미구현 | 읽기·수정·검사 도구를 모델 요청에 연결 |
| Git workspace·제한된 실행기 | 로컬 source snapshot 구현; Git/운영 격리 후속 | 제어 프로그램과 분리된 clone·변경·검사·push |
| 공통 실행기와 이 저장소의 실행 profile | 설정·command intent/실행·중단·복구·JUnit 구현; 합성 프로세스로 검증 | 실제 Python runtime 연결·테스트·평가·패키징 검증 |
| 웹·릴리스·업데이트·복구 | 미구현 | 후보 버전 평가 후 설치·정상 확인·rollback |

특정 repo를 최종적으로 활성화하려면 App 설치 외에도 허용 모델 연결, repo 정책·승인 역할, toolchain, 빌드/검증·배포 profile이 준비되어야 한다. 기존 repo 규칙은 Agent가 수집하고 확인이 필요한 누락 항목만 받는다. App만 설치했다고 readiness를 완료로 바꾸지 않는다.

## 2. 첫 적용 범위

이 Python 저장소를 개발 검증용 첫 대상으로 추가한다. Java 8/Maven/JUnit 업무 지원 목표는 유지한다. AI-DLC의 작업·승인·문서·PR 흐름은 언어 독립적이다. 공통 실행기에 repository별 명령·toolchain·검증 결과 형식을 설정하며, 새 언어마다 별도 Agent나 언어 전용 실행기를 만들 필요는 없다.

repository loader의 project.adapter에 언어 중립적인 command profile을 추가했다. [이 저장소의 설정 예시](config/repository.self.example.json)는 unittest와 로컬 평가 명령을 담는다. Maven 같은 build tool의 자동 탐색·JUnit 같은 보고서 해석은 선택적인 보조 모듈로 분리한다. Python/C#이라는 언어 이름을 지원 여부의 유일한 조건으로 삼지 않는다. 실제 실행 환경과 인수 검증 방법이 준비되었는지를 확인한다.

이 저장소의 실행 profile이 다룰 항목:

- 승인된 Python executable/version, 별도 작업 디렉터리·환경·자원 제한.
- repo 문서의 unittest 실행과 로컬 평가 CLI를 실제 argv로 실행하고 종료 코드·보고서·commit을 수집.
- 현재 테스트는 `python -m unittest discover -s tests -v`, 평가는 `python -m ai_dlc eval run --plan evaluation/core.plan.json`을 기준으로 발견.
- test/평가 통과를 wheel 생성·설치·서비스 시작까지 검증한 것으로 표시하지 않음.
- packaging은 내부에서 공급한 build tool로 수행하고 dependency를 public registry에서 자동 다운로드하지 않음.
- 실제 Agent 설치/업데이트 인수는 RHEL별 runtime·package·state migration·시작/중단·정상 판정·rollback을 별도로 검증.

## 3. 구현 순서

| 순서 | 구현 묶음 | 확인할 동작 |
| --- | --- | --- |
| 1 | task revision·승인·상태 엔진·파일 journal | 구현 시작 조건까지 완료. 외부 실행/효과 복구는 실행기와 함께 확장 |
| 2 | 제한된 작업 공간·공통 실행기·repo 실행 profile | 로컬 snapshot·command 실행/검증 구현. Git clone·운영 UID/egress 격리·실제 toolchain 연결 후속 |
| 3 | 실제 모델 protocol adapter와 tool loop | 승인된 변경 요청을 코드·테스트 변경으로 만들고 검사 결과 해석 |
| 4 | GHES App과 Issue/PR 경로 | 원문 정리 → 승인 → 구현 → 검증 → 완결된 PR |
| 5 | 자기 적용 업무 평가·독립 검토 | 동일 입력에서 재현, 실패/추가 사람 개입 기록, 잘못된 성공 판정 방지 |
| 6 | 웹·패키징·설치/업데이트·복구 | 전체 흐름을 비운영 환경에서 검증한 뒤 운영 도입 평가 |

1~4가 준비되면 개발 검증용 repo에서 실제 Issue→PR 개선을 시도할 수 있다. 이는 운영 도입 완료가 아니다. 전체 SDLC 자동화 가능성과 [운영 평가 기준](EVALUATION_SPEC.md)을 충족한 뒤 운영에 사용한다. 공통 기능은 계속 최종 코드베이스에 누적한다.

## 4. 자기 개선 실행 방식

```text
고정된 Agent A + 별도 state/설정
  → 이 저장소의 특정 commit을 후보 B 작업 공간으로 clone
  → Issue의 승인된 요구사항에 따라 B의 코드·테스트 수정
  → B 자체 테스트 + 고정된 평가자/회귀 세트로 검증
  → 완결된 PR → 기존 사람 리뷰·머지 정책
  → 검증된 새 package 생성
  → 비운영 설치·state 호환성·복구 시험
  → 운영 평가/릴리스 절차에 따라 다음 Agent 버전으로 승격
```

실행 중인 A의 설치 코드·state·secret을 B의 수정 작업 공간으로 사용하지 않는다. A와 B를 한 state directory에 동시에 실행하지 않는다. 후보의 시험용 상태·자격 증명·repo/배포 대상도 분리한다.

평가 실행기와 테스트도 이 저장소의 개선 대상이 될 수 있다. 다만 B가 바꾼 기대 결과만으로 B의 통과를 결정하지 않는다. 고정된 A측 평가 계약·회귀 자료를 후보 작업 공간 밖에 유지하고, 새 평가 기준의 변경은 별도 근거와 기존 리뷰 정책으로 검토한다. 테스트 삭제/skip·기대값 완화·승인/목적지 정책 변경은 실제 diff와 검증 근거에 드러나야 한다.

모델은 코드·테스트·정책 변경을 제안할 수 있지만 자신의 승인 근거를 만들거나 외부 연결 권한을 늘리지 못한다. 현재의 `eligible_for_release=false` 로컬 결과를 수정해 true로 바꾸는 것은 운영 인수의 대체가 아니다.

새 버전은 새 작업의 실행자로 명시적으로 승격한다. 실행 중 프로세스가 자기 파일을 덮어쓰고 hot reload하는 방식을 사용하지 않는다. 코드·상태 schema가 달라지는 업데이트는 drain·백업·migration·정상 확인·실패 시 복구 절차를 따른다.

## 5. 첫 Issue 후보

[평가 보고서에 실행 환경 정보 기록](backlog/self-apply-001-evaluation-environment.md)을 첫 사례로 준비했다. 변경 범위가 작고 평가 신뢰성을 개선하며, 현재 CLI·설정·평가 자료 구조를 실제로 이해해야 해결할 수 있는 작업이다.

지금은 로컬 Issue 초안이다. GitHub에 생성/게시하지 않았고, 현재 Agent가 이 업무를 자율 수행한 것으로 보고하지 않는다. 1~4의 기반이 준비되면 이 초안의 원문을 보존하여 실제 자기 적용 흐름에 넣는다.
