# 여러 LLM 등록·선택 상세 계약 D2

상태: D1의 단일 모델 설정을 대체하는 설계와 설정 예시. 사내 종속 계약 확인은 현재 작업에서 진행하지 않는다. provider 구현이나 실제 모델 연동이 완료되었다는 의미는 아니다.

구현 상태: registry/router와 호출 전 검사·로컬 평가, Chat Completions/Responses 및 설치 custom adapter 등록, 지속 session·호출/도구 예산·복구를 구현했다. [모델 구현 계약](MODEL_IMPLEMENTATION.md)에 wire/TLS 시험과 한계를 기록했다. 실제 provider 연동 시험과 승인·도구 loop 통합은 후속 범위다. [README.md](README.md)의 진단 명령은 외부 접속 없이 모델 선택과 설정 오류를 확인한다.

## 1. 모델과 API 분리

모델명을 구현 분기 조건으로 사용하지 않는다. Gemma4·gpt-oss·DeepSeek·Gauss는 등록 가능한 논리 모델이고, 각 모델의 API 프로토콜은 제공하는 서비스에 따라 달라질 수 있다.

```text
작업 purpose + repository 정책
  → ModelRouter
  → 등록된 model ID와 기능·한도
  → provider의 주소·인증·접속 경계
  → 프로토콜 adapter
  → 공통 ModelResponse
```

| 개념 | 내용 | 예시 |
| --- | --- | --- |
| provider | 연결할 서비스와 인증·동시성·내부/외부 경계 | internal-gemma, external-openai |
| adapter | 요청·응답·tool 결과를 변환하는 API 프로토콜 구현 | openai_chat_completions, openai_responses, custom |
| model | provider가 제공하는 실제 모델의 논리 ID, 모델명 참조와 기능·한도 | gemma4, gpt-oss, deepseek, gauss, gpt-test |
| route | 목적별로 사용할 논리 model ID | design → deepseek |
| session | 같은 모델·설정·도구 계약으로 이어지는 한 대화 단위 | model_session_id |

한 provider에 여러 model을 등록할 수 있고, 같은 모델 이름을 서로 다른 provider에서 제공할 수도 있다. 같은 서비스를 공유하는 model은 provider의 동시 요청 한도를 함께 사용한다. 모델 추가가 새로운 서비스·DB·Agent 인스턴스 추가를 뜻하지 않는다.

## 2. 목적별 선택

| purpose | 하는 일 | 도구 권한 |
| --- | --- | --- |
| requirements | 원문 정리·질문·분리안 | 읽기; 승인·수정 실행 없음 |
| design | 설계안·대안·영향 분석 | 읽기; 설계 게시와 승인은 상태 엔진 책임 |
| implementation | 코드 구현·수정 | 승인된 workspace 변경·명령 |
| test_generation | 테스트 작성·수정 | 같은 승인 범위의 변경·명령, implementation과 직렬 실행 |
| review | 변경·검증 결과 검토 | 읽기 전용; finding 제안 |
| summary | 긴 대화·실행 결과 요약 | 제공된 근거 읽기 |
| operations_analysis | 운영 신호·실패 원인 분석 | 제공된 근거 읽기; 배포·rollback 권한 없음 |

상태 phase와 purpose는 다르다. verification 중 수정은 implementation/test_generation purpose를 쓰고 실제 테스트 실행은 모델 없이 ExecutionPort가 담당한다. 승인·권한·성공 판정·merge·배포는 모든 모델에서 동일한 프로그램 코드가 처리한다.

model 선택 우선순위는 다음과 같다.

1. repository.model_routing.by_purpose[purpose]
2. repository.model_routing.default_model (null이면 다음 항목)
3. llm.routing.by_purpose[purpose]
4. llm.routing.default_model

선택 결과는 repository.allowed_models 안에 있어야 한다. 이 집합 밖이면 다른 모델을 찾아 대신 호출하지 않고 설정 오류로 처리한다. 임의 댓글·repo 코드·모델 응답이 새로운 provider/endpoint를 등록하거나 route를 바꾸지 못한다.

모든 목적에 같은 모델을 쓰려면 전역 by_purpose를 비우고 default_model 하나를 지정한다. repo별로 하나만 쓰려면 해당 repo의 default_model을 지정하고 by_purpose를 비운다. 역할 분담을 원하면 필요한 purpose만 override한다.

운영 예시의 Gemma4/DeepSeek/gpt-oss/Gauss 배치는 설정 사용법을 보여주기 위한 예시이며 모델의 상대 성능이나 검증된 업무 적합성에 대한 추천이 아니다. 실제 API 규격은 추정하지 않으며 내부 provider의 adapter/auth는 unconfigured로 둔다.

## 3. 설정 형식

연결 설정과 repository profile의 schema_version은 2로 변경한다. service·배포 profile은 변경이 없으므로 1을 유지한다. 타입마다 버전을 명시적으로 검사한다. 기존 단일 `model`과 새 `llm`을 함께 지정하면 오류다. 프로그램 출시 전의 계약 변경이며 실제 실행 자료 migration을 수행한 것은 아니다.

`llm`의 필수 필드:

- providers: provider ID → ProviderConfig 객체. ID는 고유하며 빈 값 불가.
- models: model ID → ModelConfig 객체. 각 provider 참조가 존재해야 함.
- routing: default_model(필수), by_purpose(빈 객체 허용). 등록된 model만 참조.
- failure_policy: 현재는 stop만 지원. 모델 오류를 다른 모델/외부 서비스로 자동 전환하지 않음.

ProviderConfig:

| 필드 | 계약 |
| --- | --- |
| boundary | internal / external. 운영자가 관리하는 경계 선언이며 실제 host/route 정책도 별도 충족 |
| adapter | unconfigured / openai_chat_completions / openai_responses / custom |
| adapter_id | custom인 경우만 필수. 설치 패키지에 등록한 프로토콜 구현 ID; 원격 plugin URL 아님 |
| base_url | HTTPS URL, 승인된 목적지, credential/query/fragment 금지 |
| auth | unconfigured, none, bearer, header 중 하나의 명시적 인증 객체 |
| max_concurrent_requests | 양의 정수, 모든 model이 공유하는 provider 한도 |

AuthConfig는 type별로 검증한다. unconfigured/none은 추가 필드 없음, bearer는 token_env, header는 header_name과 value_env가 필수다. 실제 secret 값을 파일에 저장하지 않는다. 복합 인증이 필요한 서비스는 검증된 custom adapter에 별도 인증 schema를 등록한다. auth=none은 운영자의 명시적인 선택이고 unconfigured와 구분한다.

ModelConfig:

| 필드 | 계약 |
| --- | --- |
| provider | 등록된 provider ID |
| model_env | 실제 배포 모델 식별자를 읽을 환경 변수 이름 |
| capabilities | tool_calls, structured_output, streaming 각각 supported / unsupported / unknown |
| context_window_tokens | 양의 정수 또는 null(미확인) |
| max_output_tokens | 양의 정수 또는 null(미확인); context보다 작아야 함 |

모델별 기능은 모델 계열명에서 유추하지 않는다. 같은 모델이라도 gateway가 노출하는 기능이 다를 수 있다. 구성/기능 값은 operator가 제공하고 실제 호출 결과가 이를 위반하면 MODEL_PROTOCOL_ERROR로 처리한다. 값이 있다고 실제 호환성 검증을 통과한 것으로 표시하지 않는다.

미확인 adapter/auth/한도는 오프라인 설계·형식 검사에서 허용하지만 상태는 configuration_pending이다. 실제 호출 전에 선택된 route의 adapter·auth·secret·모델명·문맥/출력 한도·필수 기능이 구성되어야 한다. 초기에는 예시의 placeholder hostname도 실제 값으로 교체해야 한다. 미구성을 해결하려고 API 후보 경로·다른 모델·외부 주소를 탐색하지 않는다.

실행 권한이 있는 코딩 purpose에는 확인된 tool_calls=supported가 필요하다. 도구가 없는 모델은 자료를 프로그램이 수집해 전달하는 정리/설계/리뷰 purpose에 사용할 수 있다. 구조화 결과는 schema 검증과 제한된 재요청을 거치며 provider의 native structured output 지원을 필수로 하지 않는다. 자연어를 shell 명령으로 해석하는 fallback은 제공하지 않는다. streaming은 선택 기능이며 미확인이면 non-streaming을 사용한다.

## 4. 어댑터와 Router 계약

```text
ModelRegistry.get_model(model_id) -> ModelDefinition
ModelRegistry.get_provider(provider_id) -> ProviderDefinition
ModelRouter.resolve(repo_policy, purpose, config_revision) -> ResolvedModel
ModelRouter.preflight(resolved, requirements) -> Ready | ConfigurationPending | Denied
ModelAdapter.generate(ModelRequest, ResolvedModel) -> ModelResponse
```

ResolvedModel은 model_id, provider_id, adapter/adapter_id, endpoint/auth reference, configuration digest, declared capabilities/limits를 포함한다. ModelRequest에는 이 참조와 model_session_id를 추가하고 secret 값은 공통 DTO에 넣지 않는다. adapter 구현이 secret을 transport에 적용한다. ModelResponse는 공통 텍스트·도구 요청·usage·오류·provider request ID 형식을 유지한다.

계층별 구현:

```text
models/
  types.py                # ModelRequest/Response/Definition/ResolvedModel
  registry.py             # 모델·provider 등록, 참조·설정 검사
  router.py               # 순수한 목적별 선택, allowlist·capability 검사
  sessions.py             # 모델별 대화 경계와 설정 고정
  adapters/
    openai_chat.py        # OpenAI 호환 Chat Completions 프로토콜
    openai_responses.py   # Responses 프로토콜
    custom_registry.py   # 설치 패키지의 사내 프로토콜 구현 등록
```

이 목록은 구현할 모듈 경계다. 현재 어댑터 코드나 API wire format을 제공한 것은 아니다. 내부 서비스가 OpenAI 호환이면 모델 종류와 무관하게 검증한 동일 어댑터를 선택할 수 있다. custom adapter를 새로 추가해도 상위 상태 엔진·코딩 도구·웹 구조는 변경하지 않는다.

## 5. 모델 간 문맥 전달·중단·예산

- 목적별 모델 선택은 새 model session 시작 시 확정하고 model/provider/config digest를 기록한다. 진행 중 대화에 설정 변경을 몰래 적용하지 않는다.
- 다른 모델로 넘길 때 승인된 요구사항·설계·관련 코드·현재 diff·검증 근거·정제한 요약으로 새 ContextBundle을 만든다. 원래 provider의 숨겨진 추론, opaque response ID, 전용 system message, 미완료 tool call을 다른 provider에 전달하지 않는다.
- 동일 purpose 내 모델 교체는 안전한 checkpoint에서 기존 tool invocation을 처리/중단한 뒤 새 session으로 시작한다. 이미 수행한 파일 변경·명령을 새 모델의 요청처럼 재실행하지 않는다.
- 모델 변경만으로 요구사항 승인을 자동 취소하지 않는다. 변경 결과가 승인 범위/설계 합의를 바꾸면 기존 재승인 규칙을 적용한다. 기존 테스트·사람 리뷰는 해당 commit/revision의 근거로만 유지한다.
- 코드 작성 모델과 리뷰 모델을 다르게 지정할 수 있다. 모델 리뷰는 advisory finding이며 사람 리뷰 수·요구사항 승인·GHES 보호 규칙을 충족한 것으로 계산하지 않는다.
- 여러 모델이 동시에 같은 workspace를 변경하지 못한다. test_generation은 구현 loop의 순차 하위 작업으로 호출하며 task 변경 lock을 공유한다.
- 전체/작업별 model_calls, tool_calls, 활성 시간 예산은 모든 model session을 합산한다. provider마다 재설정하거나 모델 교체로 우회하지 않는다.
- 서비스 model_concurrency와 provider.max_concurrent_requests를 모두 만족해야 호출한다. 모델별 오류/소비량을 기록하고 서로 다른 모델의 token 수를 비용처럼 단순 합산하지 않는다. 사용량 미제공은 null이다.

`failure_policy=stop`은 해당 모델 요청의 제한된 transient retry를 막는다는 뜻이 아니다. 같은 endpoint/모델 재시도는 기존 규칙을 따르되 실패하면 해당 작업을 대기/실패 상태로 남긴다. 다른 모델 선택은 운영자 설정 변경과 checkpoint 재개를 통해 명시적으로 수행한다.

## 6. 환경 경계

production에서는 모든 provider가 internal이고 URL host가 internal_hosts에 있어야 한다. external provider는 route에서 사용하지 않아도 등록 자체를 거부한다. 내부 선언만으로 외부 주소를 허용하지 않으며 runtime egress 정책까지 함께 검사한다.

test에서 external provider를 사용하려면 external_access=true와 external_hosts 일치가 필요하다. 내부 provider를 test에 등록하는 경우 internal_hosts도 명시한다. 논리 모델 별칭이나 adapter 이름에 `openai`가 포함되는지로 내부/외부를 판정하지 않는다. 반대로 사내 모델 이름을 붙인 외부 endpoint는 운영에서 허용하지 않는다.

테스트용 모델·secret·route를 production registry와 병합하지 않는다. 외부 예시의 GPT는 테스트 전용이며 사내 모델 실패 시 대체 대상으로 사용하지 않는다. 외부 테스트 자료의 범위 결정은 계속 유보한다.

## 7. 기록·웹·검증

각 모델 단독과 목적별 route 전체의 품질 비교·운영 판정은 [EVALUATION_SPEC.md](EVALUATION_SPEC.md)를 따른다. 같은 사례·정책·예산으로 비교하고 모델/provider 실제 버전과 prompt/tool/config digest를 고정한다. router 단위 시험의 성공과 실제 업무 성능을 구분한다.

각 run에 purpose별 실제 model/provider/session/config digest, 호출 수, 지연, 오류, 사용량(있을 때만)을 기록한다. 웹에는 현재 모델과 모델 변경 이력을 표시하고 endpoint secret/header·전체 프롬프트·숨겨진 추론은 공개하지 않는다. 설정되어 있지만 아직 사용할 수 없는 모델은 configuration_pending으로 표시한다.

로컬 테스트 대역으로 검증할 항목:

1. 한 모델을 모든 purpose에 사용, 서로 다른 모델을 목적별 사용, repo 우선순위와 allowed_models 거부.
2. 없는 model/provider, 알 수 없는 purpose, 중복 ID, 구/신 설정 혼합, 미구성 adapter/auth 처리.
3. 같은 adapter를 내부/외부 provider가 사용해도 실제 경계로 판단. production의 미사용 external provider도 거부.
4. tool 미지원 모델의 코딩 거부와 읽기 목적 사용, null 한도/unknown 기능의 readiness 구분.
5. 세션 교체·미완료 도구·checkpoint 복구, 실제 수행한 도구 중복 실행 방지.
6. 공유 provider/global concurrency와 모델을 바꿔도 누적되는 예산.
7. 모델 오류 시 외부/다른 provider 호출 없음, model review로 사람 승인 대체 불가.

사내 API 확인 없이 registry/router/session과 이 계약의 검증은 진행할 수 있다. 실제 provider별 연결 시험은 이후 해당 환경에서 수행하며 현재 작업의 선행 조건으로 요구하지 않는다.
