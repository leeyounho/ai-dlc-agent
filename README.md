# AI-DLC Agent

사내 standalone Agent입니다. 현재는 connection/repository/service 설정, 공통 HTTPS transport, GitHub App 인증·서명 webhook inbox·현재 Issue/댓글/권한 관측 adapter, 모델 선택, 요구사항·설계 revision과 승인, 파일 저널·복구, 별도 로컬 source snapshot, 공통 command 실행·중단·결과 검증과 로컬 평가가 구현되어 있습니다. 실제 GHES 계약 시험과 HTTP route wiring, LLM adapter 연결·Git clone/PR·운영용 실행 격리·배포·웹 서버는 후속 범위입니다.

## 실행

Python 3.12 이상이 필요합니다. 현재 핵심 모듈은 표준 라이브러리만 사용하므로 설치나 다운로드 없이 프로젝트 디렉터리에서 실행할 수 있습니다.

```text
python -m ai_dlc --help
python -m ai_dlc validate-config --config config/production.example.json --repository config/repository.example.json --compare-config config/test-external.example.json
python -m ai_dlc validate-service --service config/service.example.json
python -m ai_dlc route-model --config config/production.example.json --purpose implementation
python -m ai_dlc eval validate --plan evaluation/core.plan.json
python -m ai_dlc eval run --plan evaluation/core.plan.json
python -m ai_dlc workflow demo
python -m ai_dlc execution demo
python -m unittest discover -s tests -v
```

`validate-config`는 연결 schema 2와 선택적인 repository schema 2를 검증합니다. `validate-service`는 service schema 1과 connection/repository 참조, 관리 경로, credential 참조 및 로컬 readiness를 네트워크 없이 검사합니다. 예시에는 실제 secret/runtime profile/address range가 없으므로 구조가 유효해도 `configuration_pending`입니다. `--compare-service`는 운영/테스트의 workspace/state/log/cache/artifact 및 credential 참조가 겹치지 않는지도 검사합니다. OS 계정과 방화벽의 실제 격리 검증은 아닙니다.

`route-model`은 선택된 model/provider와 선택 근거·미구성 사유를 출력하며 모델을 호출하지 않습니다. 실제 adapter를 등록하지 않았고 예시 주소/기능/한도가 미확정이므로 현재 예시는 configuration_pending입니다. repository 예시도 disabled 상태이므로 호출 전 검사를 하면 denied로 표시합니다. 형식상 유효한 것과 호출 준비/운영 준비가 된 것을 구분합니다. `--require-ready`를 추가하면 미준비 상태에서 종료 코드 3을 반환합니다.

모델 선택 순서는 repo purpose → repo default → 전역 purpose → 전역 default입니다. 선택한 모델이 repo allowlist 밖이면 다른 모델로 대체하지 않습니다. 내부 OpenAI 호환 API는 protocol 이름 때문에 차단하지 않고 실제 host/boundary 설정으로 판단합니다.

## 로컬 평가

[core.plan.json](evaluation/core.plan.json)이 [평가 세트](evaluation/suites/core.json)를 참조합니다. 사례는 합성 fixture를 복사한 뒤 선언된 필드를 변경하고 **실제 설정 로더·Router·DispatchGuard**를 실행합니다. HTTP 대신 호출 횟수만 기록하는 로컬 adapter를 사용합니다. Python socket 생성·DNS 함수를 가로채 예상치 않은 네트워크 시도도 감지합니다. 실제 사외 주소로 차단 시험을 하지 않습니다.

검증 범위:

- 전역/repository 목적별 모델 선택 우선순위, 미등록 모델/provider, allowlist 밖 모델 거부.
- 운영 외부 연결 설정·미사용 external provider 등록·허용되지 않은 URL·redirect 거부.
- 도구 미지원 모델의 코딩 차단과 요약 목적 허용, 누락 인증/한도/adapter의 미준비 처리.
- 잘못된 자료형·알 수 없는 설정·경로 중첩·필수 요구사항 승인 해제 거부.
- adapter 오류 시 자동 모델 fallback이 발생하지 않는지 확인.

`eval run`은 실행별로 `var/evaluations/<evaluation-id>/report.json`과 `report.md`를 기록합니다. 실패해도 보고서를 남기고 종료 코드 1을 반환합니다. 보고서의 코드 digest와 suite/fixture digest를 함께 보관합니다.

```text
python -m ai_dlc eval report --evaluation var/evaluations/<evaluation-id>
python -m ai_dlc eval compare --baseline <previous-report-directory> --candidate <new-report-directory>
```

비교는 같은 평가 세트·fixture·기대 결과에 대해서만 수행합니다. 결과 집계와 사례별 판정의 불일치를 검사하지만 파일의 암호학적 서명/공증 기능은 아닙니다. 로컬 계약 평가에는 언제나 `eligible_for_release=false`를 기록합니다. 33개 사례의 통과는 실제 LLM 업무 성공률이나 운영 적합성 평가를 대신하지 않습니다.

종료 코드: 0 성공, 1 평가 실패/회귀 발견, 2 잘못된 입력·로컬 오류, 3 모델 호출 전 조건 미충족(`--require-ready`), 130 사용자 중단.

## 구현 구조와 경계

- [config](ai_dlc/config/loader.py): 중복 key·NaN·알 수 없는 필드·묵시적 자료형 변환을 거부하는 strict loader, immutable connection/repository/service 설정과 readiness.
- [transport](ai_dlc/transport.py): 정확한 HTTPS host/port, DNS 결과 CIDR, TLS/CA, redirect, timeout·응답 크기, 읽기 재시도와 쓰기 `EFFECT_UNKNOWN`을 적용하는 공통 경계.
- [github](ai_dlc/github/): App JWT와 repository-scoped installation token, 현재 API 사실을 읽는 ObservationGateway, 원본 서명 webhook의 durable inbox와 workflow event processor.
- [models](ai_dlc/models/router.py): Registry, Router, readiness, DispatchGuard. 실제 protocol adapter와 transport 연결은 아직 포함하지 않습니다.
- [workflow](ai_dlc/workflow/engine.py): 원문 보존·req/des revision, 필수 요구사항 승인, 별도 시작/자동 시작, 협의/자동 설계, 변경 시 재승인, 중지·재개·취소. 구현 시작 조건 확인까지이며 실제 도구 실행은 하지 않습니다.
- [storage](ai_dlc/storage/journal.py): 단일 인스턴스 잠금, task별 CAS·중복 이벤트 방지, immutable blob·저널·파생 snapshot 복구.
- [execution](ai_dlc/execution/coordinator.py): 승인·source/runtime digest에 묶인 command intent, 재전달 중복 방지, 중단·복구, source 변경 확인·JUnit 검증. 기본 runner는 미구성 차단이며 실제 프로세스 시험에는 내장 합성 runner만 사용합니다.
- [evaluation](ai_dlc/evaluation/runner.py): 사례 실행·평가 근거·JSON/Markdown 보고서·회귀 비교.
- [tests](tests/test_config_and_routing.py): 표준 unittest 기반 테스트. 후속 pytest에서도 실행 가능한 구조입니다.

Python HTTP 경계는 DNS 결과를 설정 CIDR과 대조하고 지정 CA로 TLS hostname을 검증하지만 OS egress 제어를 대신하지 않습니다. Git·JVM·shell·자식 프로세스는 후속 RHEL runner에서 별도 통제가 필요합니다. 현재는 소스 checkout의 `AGENTS.md` 실행이나 동적 plugin import, package 자동 다운로드, 모델 자동 탐색을 수행하지 않습니다. [transport 계약](TRANSPORT_IMPLEMENTATION.md)과 [GitHub App 계약](GITHUB_IMPLEMENTATION.md)을 참고합니다.

`workflow demo`는 합성 Issue·사람 관측으로 실제 승인/상태 엔진을 실행하고 `var/workflow-evaluations/<id>/report.json`을 남깁니다. 네트워크·모델 호출은 없고 운영 인수 판정도 아닙니다. `workflow inspect --state-root <demo가 출력한 경로> --instance local-workflow-demo --repository-id 1 --issue 1`로 현재 상태를 읽을 수 있습니다. [구현 계약과 한계](WORKFLOW_IMPLEMENTATION.md)에 승인 원본 재검증·복구·검증 범위를 기록했습니다.

repository의 `project.adapter=command`는 언어 독립적인 executable ID·argv·작업 경로·timeout 설정입니다. [이 소스의 실행 설정 예시](config/repository.self.example.json)는 운영 runner/toolchain 연결 후 사용할 명령을 담습니다. 기존 Java/Maven 설정도 유지합니다.

`execution demo`는 합성 Issue의 승인부터 실제 합성 subprocess·JUnit 판정까지 여섯 사례를 실행하고 `var/execution-evaluations/<id>/report.json`과 report.md를 남깁니다. 잘못된 결과를 실패로 잡는 사례도 포함합니다. [실행 구현 계약](EXECUTION_IMPLEMENTATION.md)에 현재 동작과 운영 runner의 연결 조건을 기록했습니다. 임의 repository 코드를 현재 계정에서 실행하는 기능으로 사용하지 않습니다.

초기 설계의 Pydantic은 외부 라이브러리 공급 전에 이 핵심 모듈의 필수 의존성으로 넣지 않았습니다. 검증 규칙과 immutable DTO는 표준 라이브러리로 구현하고, 후속 FastAPI/Pydantic 통합은 같은 계약을 사용하도록 합니다. 이는 외부 패키지 설치를 시도하지 않고 최종 핵심 코드를 개발하기 위한 구현 선택입니다.

개발 환경 검증은 Windows/Python 3.14에서 수행했습니다. Python 3.12 대상 문법은 별도로 확인하되 실제 3.12 실행과 RHEL 7/8/9 설치·동작 검증은 아직 수행하지 않았습니다. `pyproject.toml`은 패키징/console entry point를 정의하며 wheel 빌드는 승인된 내부 setuptools가 준비된 환경에서 별도로 검증합니다. 이 단계에서 pip나 사외 저장소에 접속하지 않습니다.

## 다음 구현과 설계

다음 범위는 GitHub scope event를 scheduler의 task 열거·중단과 연결하고, 내장 HTTP route 및 Git 기반 source 준비, 파일 읽기/수정 도구와 실제 모델 loop, GHES Issue→PR 연결을 구현하는 것입니다. 운영 runner의 RHEL 격리·toolchain·egress 검증도 필요합니다. 이후 부모/자식 Issue 조정, 전체 검증·merge·배포 단계와 웹을 연결합니다.

이 소스 자체를 개발 검증용 첫 대상으로 삼는 [자기 적용 계획](SELF_APPLICATION_PLAN.md)과 [첫 Issue 초안](backlog/self-apply-001-evaluation-environment.md)을 준비했습니다. GitHub App 설치만으로 동작하는 단계는 아니며, 공통 실행기·이 저장소의 실행 profile과 Issue→승인→실제 구현→검증→PR 경로를 먼저 구현해야 합니다. AI-DLC 흐름은 언어 독립적이고 실행 환경·명령·결과 해석이 repository별로 달라집니다. 후보 소스와 실행 중인 Agent·평가 기준을 분리합니다.

[기본 설계](DESIGN.md) · [상세 설계](DETAILED_DESIGN.md) · [다중 모델](MULTI_MODEL_SPEC.md) · [운영 도입 평가](EVALUATION_SPEC.md)
