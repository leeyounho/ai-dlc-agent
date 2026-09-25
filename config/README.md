# 환경별 연결 설정

현재 파일은 Agent 설정의 연결·저장 경계에 대한 예시이자 구현 계약이다. 연결/repository/service 로더와 Python HTTPS transport는 구현했고 전체 Agent는 후속 범위다. 설정 검증과 실제 외부 접속 통제의 구현·검증을 구분한다. 테스트 데이터 범위는 사용자의 지시에 따라 추후 결정하며 현재 설정 항목이나 구현 착수 조건으로 두지 않는다.

구현 갱신: 연결/repository schema 2와 service schema 1 로더, 공통 HTTPS transport, 모델 선택·호출 전 검사·로컬 평가 CLI, 설치자 소유 runner/toolchain/egress schema와 helper 제어 adapter를 구현했다. 배포/운영 평가 정책의 실행기는 후속 범위다. [실행 방법](../README.md), [RHEL runner 계약](../RHEL_RUNNER_IMPLEMENTATION.md), [transport 구현 계약](../TRANSPORT_IMPLEMENTATION.md)을 참고한다. 예시 runtime profile은 의도적으로 unverified이며 실제 API나 OS 통제를 검증한 상태가 아니다.

repository project에 언어 중립적인 `command` 프로필을 추가했다. [repository.self.example.json](repository.self.example.json)은 이 Python 저장소의 unittest·로컬 평가 명령을 보여준다. 기존 Java/Maven 프로필과 같은 workflow·승인 엔진을 사용한다. 두 예시 모두 disabled이며 실제 toolchain 등록·실행 격리·패키징·배포 연결 전에는 실행 준비가 아니다.

필드·타입·교차 검증은 [CONFIG_CONTRACT.md](CONFIG_CONTRACT.md), 여러 LLM 등록·선택은 [MULTI_MODEL_SPEC.md](../MULTI_MODEL_SPEC.md)에 정의한다. 연결/repository schema는 2, service/배포/runtime profile schema는 1이다. 연결 예시 외에 [service.example.json](service.example.json), [repository.example.json](repository.example.json), [deployment.example.json](deployment.example.json)과 [runtime 예시](../runtime/)를 제공한다. service는 운영 연결 예시를 참조하며 secrets/runtime profile/실제 address range는 설치자가 공급한다. repository·배포·runtime 예시는 disabled/unverified 상태이고 실제 ID·digest·승인 역할·배포 환경을 채우기 전 실행하지 않는다.

[evaluation.example.json](evaluation.example.json)은 별도 평가 정책(schema_version=1)의 초기 제안이다. 실제 평가 세트/scope 경로·운영 자원 목표는 아직 채우지 않았다. policy_status=proposed인 예시로 운영 적합 판정을 내릴 수 없으며, [평가 계약](../EVALUATION_SPEC.md)에 따라 정식 평가 전 정책을 채택·고정한다. 평가 실행기는 아직 구현하지 않았다.

## 프로파일

| 파일 | 목적 | 모델 | Maven | 외부 연결 |
| --- | --- | --- | --- | --- |
| production.example.json | 사내 운영 | Gemma4·gpt-oss·DeepSeek·Gauss 등록 및 목적별 선택 | 사내 저장소 | 금지 |
| test-external.example.json | 명시적으로 선택하는 외부 테스트 | GPT 테스트 model, Responses 프로토콜 선택 예시 | 예시: Maven Central | 목록에 지정한 목적지만 |

운영 예시의 .internal.example 주소는 실제로 호출할 주소가 아니다. 운영자가 승인한 내부 주소와 설정으로 교체해야 한다. 내부 provider의 API/auth는 추측하지 않고 unconfigured로 남겼다. 기능 unknown과 한도 null은 미확인 상태다. 사내 계약 확인은 현재 작업에서 진행하지 않는다. 테스트 예시의 실제 외부 주소도 이번 작업에서 호출하지 않았다. 실제 배포 모델 ID는 환경 변수로 지정한다. 목적별 모델 배치는 구성 방법의 예시이며 성능·적합성 추천이 아니다.

설정 파일을 명시적으로 선택해 provider·endpoint·Maven 저장소를 바꾼다. 자동 환경 감지나 실패 시 외부 fallback을 넣지 않는다. 운영 실행의 기본 정책은 외부 연결 금지다. 테스트 프로파일을 운영 서비스의 설정 위치에 자동 복사하거나 활성화하지 않는다.

## 값의 의미

- profile: production 또는 test. production에서 external_access=true는 잘못된 설정으로 처리한다.
- network.internal_hosts: 운영자가 확인한 내부 목적지 목록. 호스트 이름이나 private IP 여부만으로 사내 여부를 자동 판정하지 않는다.
- network.external_hosts: test 프로파일에서 명시적으로 허용한 목적지. external_access=true도 이 목록 밖 연결을 허용하지 않는다.
- llm.providers: endpoint·인증·프로토콜 어댑터·내부/외부 경계·동시성 등록. 같은 provider에 여러 모델을 연결할 수 있다.
- llm.models: 실제 모델명 환경 변수·provider 참조·기능·문맥/출력 한도. 모델 이름에서 API 규격을 추측하지 않는다.
- llm.routing: 전역 default_model과 purpose별 model 선택. repository.model_routing으로 허용된 모델 안에서 재정의할 수 있다.
- llm.failure_policy: stop. 모델 장애 시 다른 모델이나 외부 endpoint로 자동 전환하지 않는다.
- auth.token_env / value_env와 model_env는 값을 읽을 환경 변수 이름이다. 비밀값 자체를 JSON에 기록하지 않는다. 미구성 상태는 오프라인 검사에서 표시하고 실제 호출 전에 선택한 모델의 준비 상태를 검사한다.
- maven.repository_url / mirror_of: 실행용 Maven settings에 적용할 저장소와 mirror 범위. repository뿐 아니라 plugin repository와 wrapper·실행 script의 추가 목적지도 통신 정책으로 통제해야 한다.
- workspace.root: 해당 프로파일의 작업 디렉터리. 자료의 분류·승인 정책은 현재 설정에 포함하지 않는다.
- 상대 파일 경로는 선택한 설정 파일의 위치를 기준으로 해석한다. 운영·테스트의 workspace·state·log·Maven cache를 분리한다.

## 로더·실행기에 필요한 검증

1. schema·profile·필수 값·환경 변수 이름을 검증하고 알 수 없는 보안 설정을 무시하지 않는다.
2. 운영 프로파일의 외부 허용, external_hosts 및 external provider 등록을 거부한다. 내부 OpenAI 호환 프로토콜은 허용하며 실제 경계/목적지로 판정한다. 내부 목적지 목록은 코드 checkout 밖의 신뢰된 설정에서 제공한다.
3. 각 URL의 scheme·host·port와 허용 목록을 검증한다. 임의 redirect를 따라가지 않는다. 내부망 경계·DNS·proxy 및 실제 실행 통제도 별도로 적용한다.
4. 테스트 workspace·state·log·cache·secret을 운영 프로파일과 분리한다. 실제 테스트에 사용할 자료는 추후 정한다.
5. Maven을 선택한 settings와 cache로 실행한다. 외부 테스트 허용을 PyPI·OS mirror·CDN·GitHub.com·telemetry의 포괄 허용으로 확대하지 않는다.
6. 설정은 작업 시작 시 읽어 유효한 revision을 기록한다. 실행 도중 정책 파일이 바뀌면 재평가하며 기존 작업의 목적지를 조용히 변경하지 않는다.
7. 로그·웹에 현재 profile·목적별 model/provider·Maven 대상과 model session 이력을 표시하되 인증값은 표시하지 않는다. 네트워크 또는 모델 오류를 다른 환경으로의 자동 전환 이유로 삼지 않는다.
8. 프로파일 검증 실패 시 네트워크 요청을 만들지 않는다. 설정 검증·차단 시험은 로컬 테스트 대역으로 수행할 수 있다.

## 테스트의 의미

GPT API와 공개 Java fixture로 공통 Agent 흐름을 시험할 수 있다. 그 성공은 Gemma4 API 호환성, 사내 Maven 연결, 운영 데이터 경계 또는 RHEL 전 버전의 검증을 대신하지 않는다. 운영 도입 기준은 별도로 유지한다.

실제 외부 호출은 명시적으로 선택한 테스트 프로파일과 허용된 endpoint·자격 증명을 사용한다. 테스트 자료의 범위는 추후 결정 사항이며 이번 작업에서는 어떤 자료도 외부로 보내지 않았다. 현재는 설정 예시를 작성했으며 호출·다운로드·API 비용 발생 작업은 실행하지 않았다.
